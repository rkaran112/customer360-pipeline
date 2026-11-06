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
import sys
import tempfile
import threading
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st


def import_fresh_pipeline():
    """Import run_pipeline, making sure it is the CURRENT version of the code.

    WHY: a long-running server (Streamlit Cloud) can keep an OLD copy of our modules in
    memory after a new version is pulled from GitHub. The page then calls a function the old
    copy doesn't have (AttributeError). If we detect that, we drop every project module from
    memory and import them again from disk.
    """
    import run_pipeline as module
    if not hasattr(module, "execute"):
        stale = [m for m in sys.modules if m in ("run_pipeline", "pipeline") or m.startswith("pipeline.")]
        for name in stale:
            del sys.modules[name]
        import run_pipeline as module
    return module


run_pipeline = import_fresh_pipeline()
if not hasattr(run_pipeline, "execute"):
    # Still wrong after a fresh import: show WHICH file Python loaded, so it can be fixed.
    st.error(f"Wrong 'run_pipeline' module loaded from {getattr(run_pipeline, '__file__', '?')}. "
             f"Expected {Path(__file__).parent / 'run_pipeline.py'}. Reboot the app (Manage app -> Reboot).")
    st.stop()

from pipeline import chaos, diagram  # noqa: E402  (imported after the freshness check above)
from pipeline.config import PROJECT_ROOT, load_config  # noqa: E402
from pipeline.health import compute_health  # noqa: E402
from pipeline.insights import format_inr  # noqa: E402
from pipeline.outputs import for_csv, read_run_history, to_records  # noqa: E402
from pipeline.privacy import mask_email  # noqa: E402

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
    """Run the pipeline once per set of input files. `signature` is only there to bust the cache.

    execute() never raises: a failed run comes back as an outcome with the stage, error and hint,
    and is recorded in the run history (so the health panel shows it).
    """
    with pipeline_lock():
        outcome = run_pipeline.execute(input_dir=input_dir, output_dir=output_dir)
    outcome.pop("error", None)          # exception objects can't be cached; the text is in summary
    if outcome["result"] is not None:
        outcome["result"].pop("config")     # contains paths; not needed by the dashboard
    return outcome


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
OUTCOME = run_cached(str(input_dir), str(output_dir), folder_signature(input_dir))
if OUTCOME["result"] is None:
    # The run failed before producing data (bad file, schema drift, bug): explain it, then fall back.
    s = OUTCOME["summary"]
    st.error(f"**The pipeline failed in stage '{s['stage']}'** (exit code {s['exit_code']}): {s['error']}  \n"
             f"💡 **How to fix:** {s['hint']}")
    if "upload_dir" in st.session_state:
        del st.session_state["upload_dir"]
        st.info("Showing the last good results from the sample data instead.")
        output_dir = SAMPLE_OUTPUT
        OUTCOME = run_cached(str(SAMPLE_DIR), str(SAMPLE_OUTPUT), folder_signature(SAMPLE_DIR))
    if OUTCOME["result"] is None:
        st.stop()
R = OUTCOME["result"]
ALERTS = R["alerts"]

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

STATUS_STYLE = {"HEALTHY": ("🟢", st.success), "DEGRADED": ("🟠", st.warning),
                "FAILING": ("🔴", st.error), "UNKNOWN": ("⚪", st.info)}


# LIVE health strip: st.fragment(run_every=...) re-runs ONLY this small block every 5 seconds,
# re-reading run_history.jsonl - so runs started elsewhere (CLI, scheduler) show up by themselves.
@st.fragment(run_every="5s")
def live_health_strip():
    h = compute_health(read_run_history(output_dir), CONFIG)
    icon, box = STATUS_STYLE[h["status"]]
    if h["status"] == "UNKNOWN":
        box(f"{icon} Pipeline health: UNKNOWN - no runs yet")
        return
    last = h["last_run"]
    box(f"{icon} **Pipeline health: {h['status']}** · last run `{last['run_id']}` at {last['finished_at']} "
        f"({last.get('status', '')}) · success rate {h['success_rate_pct']}% over {h['window']} runs · "
        f"avg {h['avg_duration_s']}s · {'; '.join(h['reasons'])}")


live_health_strip()

# ALERTS banner: only WARNING and CRITICAL here; INFO alerts are on the Data Quality tab.
serious = [a for a in ALERTS if a["severity"] != "INFO"]
if serious:
    with st.expander(f"🚨 {len(serious)} alert(s) need attention - "
                     f"{sum(a['severity'] == 'CRITICAL' for a in serious)} critical", expanded=True):
        for a in serious:
            (st.error if a["severity"] == "CRITICAL" else st.warning)(
                f"**{a['severity']} · {a['code']}** - {a['message']}  \n💡 {a['advice']}")
if not R["ok"]:
    st.error("This run did NOT pass its quality gate, so its outputs were **not published** "
             "(the previous good outputs are kept). The numbers below are shown for investigation only.")

# key= makes Streamlit REMEMBER the open tab, so clicking a button (e.g. in the Chaos Lab)
# reruns the page without jumping back to the first tab.
tabs = st.tabs(["🔀 Pipeline", "📊 Overview", "💰 Revenue & Segments", "🧠 Analytics",
                "🎧 Support", "✅ Data Quality", "🔍 Customer Lookup", "❤️ Pipeline Health", "🧪 Chaos Lab"],
               key="main_tabs")

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

    st.markdown("**🚨 Alerts raised by this run** (limits in config.json -> alerts)")
    if ALERTS:
        table(pd.DataFrame(ALERTS)[["severity", "code", "source", "message", "value", "threshold", "advice"]])
    else:
        st.success("No alerts.")

    st.markdown("**Quarantine - every rejected record and why** (nothing is silently deleted)")
    table(R["quarantine"].rename(columns={"_source": "source", "_source_row": "record"}))


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

# ================================================================= TAB 8: PIPELINE HEALTH (live)
with tabs[7]:
    st.subheader("Pipeline health - live")
    st.caption("Refreshes every 5 seconds from output/run_history.jsonl. Every run is recorded, good or failed: "
               "from this dashboard, the command line, a scheduler, or a chaos test.")

    if st.button("▶️ Run the pipeline now", help="Runs the pipeline on the current data and records the run"):
        with pipeline_lock():
            now = run_pipeline.execute(input_dir=input_dir, output_dir=output_dir)
        (st.success if now["ok"] else st.error)(
            f"Run {now['run_id']} finished with exit code {now['exit_code']} ({now['summary']['status']})")

    @st.fragment(run_every="5s")
    def health_panel():
        history = read_run_history(output_dir)
        h = compute_health(history, CONFIG)
        icon, box = STATUS_STYLE[h["status"]]
        box(f"### {icon} {h['status']}\n" + "\n".join(f"- {r}" for r in h["reasons"]))
        if not history:
            return
        k = st.columns(5)
        k[0].metric("Runs recorded", h["runs"])
        k[1].metric(f"Success rate (last {h['window']})", f"{h['success_rate_pct']}%")
        k[2].metric("Hours since last good run", h["hours_since_success"] if h["hours_since_success"] is not None else "-")
        k[3].metric("Avg duration", f"{h['avg_duration_s']} s")
        k[4].metric("Slowest run", f"{h['max_duration_s']} s")

        runs = pd.DataFrame(history[-CONFIG["health"]["window_runs"]:])
        runs["run"] = range(1, len(runs) + 1)
        runs["result"] = runs["ok"].map({True: "success", False: "failed"})
        runs["duration_seconds"] = runs["duration_seconds"].fillna(0)
        left, right = st.columns(2)
        with left:
            st.markdown("**Run timeline** (red = failed)")
            chart = alt.Chart(runs).mark_bar().encode(
                x=alt.X("run:O", title="run"), y=alt.Y("duration_seconds:Q", title="seconds"),
                color=alt.Color("result:N", scale=alt.Scale(domain=["success", "failed"], range=["#43A047", "#E53935"])),
                tooltip=[c for c in ["run_id", "finished_at", "status", "exit_code", "duration_seconds", "error"]
                         if c in runs.columns])
            st.altair_chart(chart.properties(height=240), width="stretch")
        with right:
            st.markdown("**DQ score per run**")
            scored = runs.dropna(subset=["dq_score_pct"]) if "dq_score_pct" in runs else runs.head(0)
            if len(scored):
                st.altair_chart(alt.Chart(scored).mark_line(point=True).encode(
                    x="run:O", y=alt.Y("dq_score_pct:Q", scale=alt.Scale(zero=False), title="DQ score %"),
                    tooltip=["run_id", "dq_score_pct", "quarantined_records"]).properties(height=240), width="stretch")

        if h["last_failure"]:
            f = h["last_failure"]
            st.markdown("**Last failure**")
            st.error(f"Run `{f['run_id']}` at {f['finished_at']} - **{f.get('status')}** "
                     f"(exit {f.get('exit_code')}) in stage **{f.get('stage', 'quality gate')}**: "
                     f"{f.get('error') or 'CRITICAL alert / failed check'}  \n"
                     f"💡 {f.get('hint') or 'See the alerts of that run.'}  \n"
                     f"🧾 crash report: `{f.get('crash_report', 'n/a (quality failure, outputs kept in failed_runs/)')}`")

        cols = [c for c in ["run_id", "finished_at", "status", "exit_code", "duration_seconds", "total_records",
                            "quarantined_records", "dq_score_pct", "retries", "alerts", "stage", "error"] if c in runs]
        st.markdown("**Recent runs**")
        table(runs[cols].iloc[::-1].astype(str))

    health_panel()

# ================================================================= TAB 9: CHAOS LAB
with tabs[8]:
    st.subheader("🧪 Chaos Lab - break it on purpose, watch it recover")
    st.caption("Each scenario runs in a private sandbox copy of the data: a good baseline run, then the fault, "
               "then a clean run to prove recovery. Your real data and outputs are never touched. "
               "Same engine as `python chaos_test.py`.")
    catalogue = pd.DataFrame([{"scenario": s.name, "what breaks": s.fault, "expected exit": s.expected_exit,
                               "must raise": s.expected_signal, "how the pipeline protects itself": s.recovery}
                              for s in chaos.SCENARIOS])
    table(catalogue)

    left, right = st.columns([3, 1])
    pick = left.selectbox("Scenario", [s.name for s in chaos.SCENARIOS], index=1, key="chaos_pick",
                          format_func=lambda n: f"{n} - {chaos.BY_NAME[n].title}")
    run_one = right.button("💥 Run scenario", width="stretch")
    run_every = right.button("Run all 13", width="stretch")

    if run_one or run_every:
        names = [s.name for s in chaos.SCENARIOS] if run_every else [pick]
        with st.spinner(f"Injecting {len(names)} fault(s)..."), pipeline_lock():
            # The lock: chaos scenarios swap code for a moment, so no other run may happen meanwhile.
            st.session_state["chaos_results"] = chaos.run_all(names, SAMPLE_DIR)

    for r in st.session_state.get("chaos_results", []):
        icon = "✅" if r["passed"] else "❌"
        with st.expander(f"{icon} {r['title']} - exit {r['actual_exit']} (expected {r['expected_exit']}) · "
                         f"health after fault + recovery: {r['health_after']} · {r['seconds']}s",
                         expanded=len(st.session_state["chaos_results"]) == 1):
            st.markdown(f"**Fault injected:** {r['fault']}  \n**Protection:** {r['recovery']}")
            if r["error"]:
                st.error(f"Stage **{r['stage']}** · {r['error']}  \n💡 {r['hint']}")
            for a in r["alerts"]:
                (st.error if a["severity"] == "CRITICAL" else st.warning)(f"{a['severity']} · {a['code']} - {a['message']}")
            table(pd.DataFrame(r["checks"]).assign(passed=lambda d: d["passed"].map({True: "✅", False: "❌"})))
            timeline = pd.DataFrame(r["history"])[["run_id", "status", "exit_code"]]
            timeline.insert(0, "step", ["baseline", "fault injected", "recovery"][: len(timeline)])
            st.markdown("**Sandbox run history** - good → fault → recovered")
            table(timeline.astype(str))
    if st.session_state.get("chaos_results"):
        results = st.session_state["chaos_results"]
        st.metric("Scenarios handled correctly", f"{sum(r['passed'] for r in results)}/{len(results)}")
