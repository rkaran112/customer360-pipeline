"""
DASHBOARD - the business view of the Customer 360 pipeline.

Run locally:   streamlit run streamlit_app.py
Online:        the same file is the "main file" on Streamlit Community Cloud.

How it gets its data (decision D-16):
    The dashboard RUNS the pipeline itself (cached), instead of expecting files to
    exist already. So the deployed website works from a fresh copy of the repo, and
    a user can upload their own 3 files and see the whole pipeline run on them.

Tabs:  Pipeline | Overview | Revenue & Segments | Analytics | Support | Data Quality | Customer Lookup
"""
import hashlib
import json
import tempfile
import threading
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import run_pipeline
from pipeline import diagram
from pipeline.config import PROJECT_ROOT, load_config
from pipeline.insights import format_inr
from pipeline.outputs import for_csv, read_run_history, to_records
from pipeline.privacy import mask_email

st.set_page_config(page_title="Customer 360", page_icon="🧭", layout="wide")

CONFIG = load_config()
SAMPLE_DIR = CONFIG["input_dir"]
SAMPLE_OUTPUT = CONFIG["output_dir"]

# What each stage does, in plain words (shown under the pipeline picture).
# >>> TO CHANGE: edit these sentences if you change what a stage does.
STAGE_EXPLAINED = {
    "Extract": "Reads crm_customers.json, orders.csv and support_tickets.xml. Every value is kept as text so "
               "loading can never fail. Each record is tagged with its file and record number (for lineage).",
    "Standardize": "Renames cust_id / customerId to customer_id (one schema), trims spaces, lowercases emails, "
                   "title-cases cities, converts numbers and dates. Checks the columns against the data contract "
                   "(schema drift guard). No row is removed here.",
    "Quality gate": "Runs 5 rules in order: invalid customer id, duplicate, missing mandatory field, invalid email, "
                    "negative revenue. The first rule a record breaks is its reason. Failed records go to quarantine.",
    "Merge": "CRM LEFT JOIN orders LEFT JOIN tickets on customer_id = one row per customer. Customers without "
             "orders or tickets are kept and flagged. Records whose customer was quarantined are counted as orphans.",
    "Insights": "Adds segment (Gold/Silver/Bronze), RFM group, average order value, days since last order, and "
                "builds 12 report tables plus headline findings.",
    "Checks": "Proves nothing was lost: records reconcile, one row per customer, revenue conserved, no negative "
              "revenue, everyone segmented. Any failure = exit code 1 and a red badge here.",
}


# ----------------------------------------------------------------- running the pipeline (cached)

def folder_signature(folder):
    """A fingerprint of the input files. Same files -> same fingerprint -> reuse the cached run."""
    digest = hashlib.md5()
    for source in CONFIG["input_sources"]:
        path = Path(folder) / source["file"]
        digest.update(path.read_bytes() if path.exists() else b"missing")
    return digest.hexdigest()


# WHY a lock: on Streamlit Cloud every visitor shares ONE server process. If two people open
# the app at the same moment, both could run the pipeline and write output/ at the same time.
# The lock makes the second run wait for the first (it takes under a second).
@st.cache_resource
def pipeline_lock():
    return threading.Lock()


@st.cache_data(show_spinner="Running the pipeline...")
def run_cached(input_dir, output_dir, signature):
    """Run the pipeline once per set of input files. `signature` is only there to bust the cache."""
    with pipeline_lock():
        result = run_pipeline.run(input_dir=input_dir, output_dir=output_dir)
    result.pop("config")     # contains paths; not needed by the dashboard
    return result


def save_uploads(uploads):
    """Save the 3 uploaded files into a temp folder, using the file names config.json expects."""
    folder = Path(tempfile.mkdtemp(prefix="c360_upload_"))
    for source in CONFIG["input_sources"]:
        (folder / source["file"]).write_bytes(uploads[source["name"]].getvalue())
    return folder


# ----------------------------------------------------------------- sidebar: data source + downloads

with st.sidebar:
    st.header("🧭 Customer 360")
    st.caption("JSON + CSV + XML -> one trusted customer view")

    with st.expander("📤 Run the pipeline on your own files"):
        st.caption("Upload all 3 files. They must follow the same layout as the sample files.")
        uploads = {}
        for source in CONFIG["input_sources"]:
            uploads[source["name"]] = st.file_uploader(
                f"{source['system']} ({source['format'].upper()})", type=[source["format"]], key=f"up_{source['name']}")
        if st.button("Run pipeline on uploaded files", disabled=not all(uploads.values()), width="stretch"):
            st.session_state["upload_dir"] = str(save_uploads(uploads))
        if "upload_dir" in st.session_state and st.button("Back to sample data", width="stretch"):
            del st.session_state["upload_dir"]

# WHAT: choose which input folder to run on (uploaded or sample), then run (or reuse the cache).
input_dir = Path(st.session_state.get("upload_dir", SAMPLE_DIR))
output_dir = input_dir / "output" if "upload_dir" in st.session_state else SAMPLE_OUTPUT
try:
    R = run_cached(str(input_dir), str(output_dir), folder_signature(input_dir))
except Exception as error:   # at the UI boundary we show ANY pipeline error instead of a crash page
    st.error(f"The pipeline could not run on these files: {error}")
    if "upload_dir" in st.session_state:
        del st.session_state["upload_dir"]
        st.info("Switched back to the sample data.")
        R = run_cached(str(SAMPLE_DIR), str(SAMPLE_OUTPUT), folder_signature(SAMPLE_DIR))
    else:
        st.stop()

C360 = R["customer_360"]
DQ = R["dq_report"]
INS = R["insights"]
STAGES = R["run_metrics"]["stages"]

with st.sidebar:
    st.subheader("⬇️ Downloads")
    st.download_button("customer_360.csv", for_csv(C360).to_csv(index=False), "customer_360.csv", "text/csv",
                       width="stretch")
    st.download_button("customer_360.json", json.dumps(to_records(C360), indent=2), "customer_360.json",
                       "application/json", width="stretch")
    st.download_button("dq_report.json", json.dumps(DQ, indent=2, default=str), "dq_report.json",
                       "application/json", width="stretch")
    st.download_button("quarantine.csv", for_csv(R["quarantine"]).to_csv(index=False), "quarantine.csv",
                       "text/csv", width="stretch")
    st.caption(f"Data: {'uploaded files' if 'upload_dir' in st.session_state else 'sample files in data/raw'}  \n"
               f"As-of date: {DQ.get('as_of_date')}")


# ----------------------------------------------------------------- small chart helpers

def bar(df, category, value, title=None, horizontal=True, colour="#1E88E5", height=320):
    """A sorted bar chart (biggest first) with tooltips."""
    if horizontal:
        enc = dict(y=alt.Y(f"{category}:N", sort="-x", title=None), x=alt.X(f"{value}:Q", title=title or value))
    else:
        enc = dict(x=alt.X(f"{category}:N", sort="-y", title=None), y=alt.Y(f"{value}:Q", title=title or value))
    chart = alt.Chart(df).mark_bar(color=colour).encode(**enc, tooltip=list(df.columns)).properties(height=height)
    st.altair_chart(chart, width="stretch")


def table(df, height=None):
    st.dataframe(df, hide_index=True, width="stretch", height=height or "auto")


# ----------------------------------------------------------------- header

st.title("🧭 Customer 360 - unified customer view")
checks_ok = all(c["ok"] for c in R["run_metrics"]["checks"])
status = (f"✅ All {len(R['run_metrics']['checks'])} checks passed" if checks_ok else "❌ Checks FAILED - do not use these numbers")
st.caption(f"{status} · DQ score {DQ['dq_score_pct']}% · {DQ['total_records']} records -> "
           f"{len(C360)} customers · ran in {R['run_metrics']['duration_seconds']}s")

tabs = st.tabs(["🔀 Pipeline", "📊 Overview", "💰 Revenue & Segments", "🧠 Analytics",
                "🎧 Support", "✅ Data Quality", "🔍 Customer Lookup"])

# ================================================================= TAB 1: PIPELINE
with tabs[0]:
    st.subheader("How the data flows - live numbers from the last run")
    left, right = st.columns([3, 2])
    with left:
        st.graphviz_chart(diagram.build_dot(R["run_metrics"], DQ, CONFIG), width="stretch")
    with right:
        st.markdown("**Read it top to bottom**")
        for i, (stage, text) in enumerate(STAGE_EXPLAINED.items(), start=1):
            st.markdown(f"**{i}. {stage}** - {text}")

    st.markdown("**Stage by stage**")
    stage_df = pd.DataFrame([{"#": i, "stage": s["stage"], "what happens": STAGE_EXPLAINED.get(s["stage"], s["description"]),
                              "rows in": s["rows_in"], "rows out": s["rows_out"], "rejected": s["rows_rejected"],
                              "ms": s["duration_ms"], "status": "✅" if s["ok"] else "❌"}
                             for i, s in enumerate(STAGES, start=1)])
    table(stage_df)

    st.markdown("**Reconciliation checks (step 6)**")
    table(pd.DataFrame([{"check": c["name"], "result": "✅ PASS" if c["ok"] else "❌ FAIL", "details": c["details"]}
                        for c in R["run_metrics"]["checks"]]))

    st.markdown("**🔎 Record lineage tracer** - follow one customer through every file and stage")
    lineage = R["lineage"]
    ids = sorted(lineage["customer_id"].dropna().unique())
    blank = lineage["customer_id"].isna()
    choice = st.selectbox("Customer id", ids + (["(records with a blank id)"] if blank.any() else []),
                          index=ids.index("C0420") if "C0420" in ids else 0, key="trace_id")
    rows = lineage[blank] if choice.startswith("(") else lineage[lineage["customer_id"] == choice]
    for source in CONFIG["input_sources"]:
        found = rows[rows["_source"] == source["name"]]
        if found.empty:
            st.warning(f"**{source['file']}** - no record for this customer")
        for _, r in found.iterrows():
            text = f"**{r['file']}** record {r['_source_row']} -> extracted -> standardized -> "
            if r["status"] == "QUARANTINED":
                st.error(text + f"**QUARANTINED** ({r['reason']}: {r['detail']})")
            elif r["status"] == "MERGED":
                st.success(text + "passed quality gate -> **MERGED into customer_360**")
            else:
                st.warning(text + f"passed quality gate -> **{r['status']}**")

# ================================================================= TAB 2: OVERVIEW
with tabs[1]:
    revenue = float(C360["lifetime_value"].sum())
    orders = float(C360["total_orders"].sum())
    k = st.columns(6)
    k[0].metric("Customers", f"{len(C360):,}")
    k[1].metric("Total revenue", format_inr(revenue))
    k[2].metric("Avg lifetime value", format_inr(revenue / max(len(C360), 1)))
    # sum / sum, NOT the average of averages - big customers must weigh more.
    k[3].metric("Avg order value", format_inr(revenue / orders) if orders else "-")
    k[4].metric("> 5 tickets", f"{100 * C360['high_ticket_customer'].mean():.0f}%")
    k[5].metric("DQ score", f"{DQ['dq_score_pct']}%")

    st.markdown("#### 💡 What the data says")
    for line in R["headlines"]:
        st.markdown(f"- {line}")

    left, right = st.columns(2)
    with left:
        st.markdown("**Customers by segment**")
        bar(INS["segment_summary"], "segment", "customers", horizontal=False, colour="#F9A825")
    with right:
        st.markdown("**Revenue by city**")
        bar(INS["revenue_by_city"], "city", "revenue", "revenue (Rs)")

# ================================================================= TAB 3: REVENUE & SEGMENTS
with tabs[2]:
    st.subheader(f"Top {CONFIG['insights']['top_n']} customers by revenue")
    left, right = st.columns([3, 2])
    with left:
        top = INS["top_customers"].assign(label=lambda d: d["customer_id"] + " " + d["name"])
        bar(top[["label", "lifetime_value", "segment", "city"]], "label", "lifetime_value", "lifetime value (Rs)")
    with right:
        table(INS["top_customers"][["rank", "customer_id", "name", "city", "lifetime_value", "total_orders"]])

    st.subheader("Customer segments")
    seg = CONFIG["segments"]
    st.caption(f"Gold: revenue > {seg['gold_above']:,} · Silver: {seg['silver_from']:,} - {seg['gold_above']:,} · "
               f"Bronze: < {seg['silver_from']:,} · thresholds come from config.json")
    left, right = st.columns(2)
    with left:
        donut = alt.Chart(INS["segment_summary"]).mark_arc(innerRadius=60).encode(
            theta="customers:Q", color=alt.Color("segment:N", scale=alt.Scale(
                domain=["Gold", "Silver", "Bronze", "No order data"],
                range=["#F9A825", "#90A4AE", "#8D6E63", "#E0E0E0"])),
            tooltip=list(INS["segment_summary"].columns)).properties(height=300)
        st.altair_chart(donut, width="stretch")
    with right:
        table(INS["segment_summary"])

    st.subheader("Regional analytics")
    left, right = st.columns(2)
    with left:
        st.markdown("**Revenue by city**")
        bar(INS["revenue_by_city"], "city", "revenue", "revenue (Rs)")
    with right:
        st.markdown("**Customer count by city**")
        bar(INS["customers_by_city"], "city", "customers", colour="#43A047")

# ================================================================= TAB 4: ANALYTICS
with tabs[3]:
    st.subheader("RFM segmentation - who to call, reward, or win back")
    st.caption("Recency (how recently), Frequency (how often), Monetary (how much) - each scored 1-3 by thirds.")
    left, right = st.columns([2, 3])
    with left:
        bar(INS["rfm_summary"], "rfm_label", "customers", colour="#8E24AA", height=260)
    with right:
        table(INS["rfm_summary"])

    left, right = st.columns(2)
    with left:
        st.markdown("**Pareto: how concentrated is revenue?**")
        curve = INS["pareto_curve"]
        line = alt.Chart(curve).mark_line(color="#1E88E5").encode(
            x=alt.X("customer_share_pct:Q", title="% of customers (biggest first)"),
            y=alt.Y("revenue_share_pct:Q", title="% of revenue"))
        diag = alt.Chart(pd.DataFrame({"x": [0, 100], "y": [0, 100]})).mark_line(
            strokeDash=[4, 4], color="#BDBDBD").encode(x="x:Q", y="y:Q")
        st.altair_chart((line + diag).properties(height=300), width="stretch")
        st.caption("The dashed line is 'every customer brings the same'. The further the blue curve bends above "
                   "it, the more revenue depends on a few customers.")
    with right:
        st.markdown("**Do high-value customers complain more?**")
        scatter = alt.Chart(C360[C360["has_order_data"]]).mark_circle(size=40, opacity=0.6).encode(
            x=alt.X("lifetime_value:Q", title="lifetime value (Rs)"),
            y=alt.Y("support_tickets:Q", title="support tickets"),
            color="segment:N", tooltip=["customer_id", "name", "segment", "lifetime_value", "support_tickets"])
        st.altair_chart(scatter.properties(height=300), width="stretch")
        table(INS["support_vs_value"])

    st.markdown("**At-risk VIPs** - Gold/Silver customers with no order for "
                f"{CONFIG['insights']['at_risk_days']}+ days (win-back call list)")
    table(INS["at_risk_vips"], height=250)

    left, right = st.columns(2)
    with left:
        st.markdown("**Registration cohorts** - customers joining per month")
        cohort = alt.Chart(INS["registration_cohorts"]).mark_bar(color="#00897B").encode(
            x=alt.X("registration_month:N", title=None), y="customers:Q", tooltip=list(INS["registration_cohorts"].columns))
        st.altair_chart(cohort.properties(height=260), width="stretch")
    with right:
        st.markdown("**City scorecard**")
        table(INS["city_scorecard"])

# ================================================================= TAB 5: SUPPORT
with tabs[4]:
    threshold = CONFIG["insights"]["high_ticket_threshold"]
    high = INS["high_ticket_customers"]
    k = st.columns(3)
    k[0].metric(f"Customers with > {threshold} tickets", f"{len(high):,}")
    k[1].metric("Total tickets", f"{int(C360['support_tickets'].sum()):,}")
    k[2].metric("Gold customers with > 5 tickets", f"{len(INS['unhappy_high_value']):,}")

    left, right = st.columns(2)
    with left:
        st.markdown("**How many tickets do customers raise?**")
        dist = C360.groupby("support_tickets", as_index=False)["customer_id"].count().rename(
            columns={"customer_id": "customers"})
        chart = alt.Chart(dist).mark_bar().encode(
            x=alt.X("support_tickets:O", title="tickets"), y="customers:Q",
            color=alt.condition(alt.datum.support_tickets > threshold, alt.value("#E53935"), alt.value("#90A4AE")),
            tooltip=["support_tickets", "customers"])
        st.altair_chart(chart.properties(height=280), width="stretch")
        st.caption(f"Red bars = more than {threshold} tickets.")
    with right:
        st.markdown("**🚨 Escalate first: Gold customers with many tickets**")
        table(INS["unhappy_high_value"], height=320)

    st.markdown(f"**All customers with more than {threshold} tickets**")
    cities = st.multiselect("Filter by city", sorted(high["city"].unique()), key="support_city")
    table(high[high["city"].isin(cities)] if cities else high, height=300)

# ================================================================= TAB 6: DATA QUALITY
with tabs[5]:
    st.subheader("Data quality report")
    k = st.columns(7)
    for col, (label, key) in zip(k, [("Total records", "total_records"), ("Duplicates", "duplicates"),
                                     ("Invalid emails", "invalid_emails"), ("Missing fields", "missing_fields"),
                                     ("Invalid IDs", "invalid_customer_ids"), ("Negative revenue", "negative_revenue"),
                                     ("Successful", "successful_records")]):
        col.metric(label, f"{DQ[key]:,}")

    failed = DQ["total_records"] - DQ["successful_records"]
    (st.success if DQ["reconciled"] else st.error)(
        f"Reconciliation: {DQ['total_records']} total = {DQ['duplicates']} duplicates + {DQ['invalid_emails']} invalid emails "
        f"+ {DQ['missing_fields']} missing fields + {DQ['invalid_customer_ids']} invalid ids + {DQ['negative_revenue']} "
        f"negative revenue + {DQ['successful_records']} successful  ->  {'balanced ✅' if DQ['reconciled'] else 'NOT balanced ❌'}")

    st.markdown("**DQ score per source system** (share of records that passed every rule)")
    cols = st.columns(len(DQ["by_source"]))
    names = {s["name"]: f"{s['system']} ({s['file']})" for s in CONFIG["input_sources"]}
    for col, (name, s) in zip(cols, DQ["by_source"].items()):
        col.metric(names.get(name, name), f"{s['dq_score_pct']}%", f"-{s['quarantined']} quarantined", delta_color="inverse")
        col.progress(min(s["dq_score_pct"] / 100, 1.0))

    left, right = st.columns(2)
    with left:
        st.markdown("**Schema drift guard**")
        if DQ["schema_drift_warnings"]:
            for w in DQ["schema_drift_warnings"]:
                st.warning(w)
        else:
            st.success("All sources match their data contract (expected columns in config.json).")
        st.markdown("**Referential integrity**")
        table(pd.DataFrame([{"check": k.replace("_", " "), "count": v} for k, v in DQ["referential_integrity"].items()]))
    with right:
        st.markdown("**dq_report.json** (format from the brief + extras)")
        st.json(DQ, expanded=1)

    st.markdown("**Quarantine - every rejected record and why** (nothing is silently deleted)")
    table(R["quarantine"].rename(columns={"_source": "source", "_source_row": "record"}))

    history = read_run_history(output_dir)
    if len(history) > 1:
        st.markdown("**Run history** - DQ score and duration of every run")
        h = pd.DataFrame(history)
        h["run"] = range(1, len(h) + 1)
        left, right = st.columns(2)
        left.line_chart(h, x="run", y="dq_score_pct")
        right.line_chart(h, x="run", y="duration_seconds")

# ================================================================= TAB 7: CUSTOMER LOOKUP
with tabs[6]:
    st.subheader("Find one customer - their full 360° view")
    query = st.text_input("Search by customer id or name", "C0001", key="lookup").strip().lower()
    hits = C360[C360["customer_id"].str.lower().str.contains(query, regex=False) |
                C360["name"].str.lower().str.contains(query, regex=False)] if query else C360.head(0)
    if hits.empty:
        st.info("No customer found. If they are missing, try the lineage tracer on the Pipeline tab - "
                "their record may be in quarantine.")
    else:
        pick = st.selectbox(f"{len(hits)} match(es)", hits["customer_id"] + " - " + hits["name"], key="lookup_pick")
        c = hits[hits["customer_id"] == pick.split(" - ")[0]].iloc[0]
        st.markdown(f"### {c['name']}  ·  `{c['customer_id']}`  ·  {c['segment']}  ·  {c['rfm_label']}")
        st.caption(f"📧 {mask_email(c['email'])}  ·  📍 {c['city']}  ·  registered {c['registration_date']:%d %b %Y}")
        k = st.columns(5)
        k[0].metric("Lifetime value", format_inr(float(c["lifetime_value"])))
        k[1].metric("Orders", int(c["total_orders"]))
        k[2].metric("Avg order value", format_inr(float(c["avg_order_value"])) if pd.notna(c["avg_order_value"]) else "-")
        k[3].metric("Support tickets", int(c["support_tickets"]), "high" if c["high_ticket_customer"] else None,
                    delta_color="inverse")
        k[4].metric("Days since last order", "-" if pd.isna(c["days_since_last_order"]) else int(c["days_since_last_order"]))
        if not c["has_order_data"]:
            st.warning("No usable order data for this customer (missing or quarantined) - segment is 'No order data'.")
        if not c["has_ticket_data"]:
            st.warning("No usable support data for this customer.")
