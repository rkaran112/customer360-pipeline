"""
STEP 5 - INSIGHTS: turn the Customer 360 table into answers for the business.

Input : customer_360 from step 4
Output: 1) customer_360 ENRICHED with new columns:
              segment                Gold / Silver / Bronze / No order data      (brief)
              avg_order_value        lifetime_value / total_orders
              days_since_last_order  how long since they last bought
              high_ticket_customer   True if more than 5 support tickets
              rfm_r/f/m, rfm_label   RFM segmentation (see below)
        2) one small table per business question (the INSIGHTS dict at the bottom)
        3) headline sentences computed from the numbers (never typed in by hand)

Required by the brief: top 10 by revenue, Gold/Silver/Bronze, >5 tickets,
revenue by city, customer count by city. Everything else is an extra.
"""
import math

import numpy as np
import pandas as pd

from pipeline.logger import get_logger

log = get_logger()

NO_ORDER_DATA = "No order data"

# What the business should DO with each RFM group.
# >>> TO CHANGE: rename a group or change its advice here.
RFM_ACTIONS = {
    "Champions": "Recent, frequent, high spend: reward them, early access, referral asks",
    "Loyal": "Recent with medium value: upsell and cross-sell",
    "Promising": "Recent but low value so far: nudge the next purchase",
    "At risk": "Valuable but gone quiet: win-back call or offer now",
    "Hibernating": "Low value and quiet: cheap reactivation email only",
}


# ----------------------------------------------------------------- small helpers

def format_inr(amount):
    """125000 -> 'Rs 1.25 lakh', 19400000 -> 'Rs 1.94 crore' (Indian number words)."""
    if amount >= 1e7:
        return f"Rs {amount / 1e7:,.2f} crore"
    if amount >= 1e5:
        return f"Rs {amount / 1e5:,.2f} lakh"
    return f"Rs {amount:,.0f}"


def resolve_as_of_date(c360, config):
    """The 'today' used for 'days since last order'.

    WHY not the real clock (D-11)? If we used today's date, the same input would give
    different numbers tomorrow, and tests would break. We use config "as_of_date", or,
    if it is null, the latest date found in the data itself.
    """
    if config.get("as_of_date"):
        return pd.Timestamp(config["as_of_date"])
    latest = pd.concat([c360["last_order_date"], c360["last_ticket_date"]]).max()
    return None if pd.isna(latest) else latest


def segment_customers(c360, config):
    """Gold if revenue > 100000, Silver if 50000..100000, Bronze if < 50000 (D-12).

    Customers with no order data get 'No order data' instead of Bronze (D-07): we do
    not know their revenue, so calling them low-value would be a guess.
    """
    gold_above = config["segments"]["gold_above"]
    silver_from = config["segments"]["silver_from"]
    revenue = c360["lifetime_value"].to_numpy(dtype=float)
    has_orders = c360["has_order_data"].to_numpy(dtype=bool)
    # np.select checks the conditions in order and takes the first that is True.
    # >>> TO CHANGE: thresholds live in config.json "segments". A new tier (e.g. Platinum)
    #     = one more condition + label here, plus its threshold in config.json.
    return np.select(
        [~has_orders, revenue > gold_above, revenue >= silver_from],
        [NO_ORDER_DATA, "Gold", "Silver"],
        default="Bronze",
    )


def tertile_score(values):
    """Score 1, 2 or 3 by thirds: top third of values = 3, bottom third = 1.

    Uses rank-percent instead of fixed cut points, so it works for any data size
    (even 1 or 2 customers), and ties are split by row order so the result is stable.
    """
    pct = values.rank(method="first", pct=True)
    return np.ceil(pct * 3).fillna(1).astype(int)


def rfm_label(r, f, m):
    """Turn the 3 scores into one easy group name. Every customer gets exactly one."""
    value = round((f + m) / 2)          # frequency and money together = how valuable
    if r >= 2:                          # bought recently
        return {3: "Champions", 2: "Loyal"}.get(value, "Promising")
    return "At risk" if value >= 2 else "Hibernating"


# ----------------------------------------------------------------- enrich the 360 table

def enrich(c360, config):
    """Add the analysis columns to customer_360."""
    df = c360.copy()
    as_of = resolve_as_of_date(df, config)
    threshold = config["insights"]["high_ticket_threshold"]

    df["segment"] = segment_customers(df, config)

    # WHY .where(total_orders > 0): never divide by zero; 0 orders -> empty value.
    df["avg_order_value"] = (df["lifetime_value"] / df["total_orders"]).where(df["total_orders"] > 0).round(2)

    if as_of is not None:
        df["days_since_last_order"] = (as_of - df["last_order_date"]).dt.days.astype("Int64")
        df["days_since_last_ticket"] = (as_of - df["last_ticket_date"]).dt.days.astype("Int64")
    else:
        df["days_since_last_order"] = pd.array([pd.NA] * len(df), dtype="Int64")
        df["days_since_last_ticket"] = pd.array([pd.NA] * len(df), dtype="Int64")

    # "More than 5" is STRICT (D-13): exactly 5 tickets is NOT a high-ticket customer.
    df["high_ticket_customer"] = df["support_tickets"] > threshold

    # RFM = Recency (how recently), Frequency (how often), Monetary (how much).
    # Each scored 1-3 among customers who have order data. Recency is "fewer days = better",
    # so we score the NEGATIVE of days.
    buyers = df["has_order_data"]
    df["rfm_r"] = pd.array([pd.NA] * len(df), dtype="Int64")
    df["rfm_f"] = pd.array([pd.NA] * len(df), dtype="Int64")
    df["rfm_m"] = pd.array([pd.NA] * len(df), dtype="Int64")
    df["rfm_label"] = NO_ORDER_DATA
    if buyers.any():
        b = df[buyers]
        df.loc[buyers, "rfm_r"] = tertile_score(-b["days_since_last_order"].astype("Float64")).to_numpy()
        df.loc[buyers, "rfm_f"] = tertile_score(b["total_orders"].astype("Float64")).to_numpy()
        df.loc[buyers, "rfm_m"] = tertile_score(b["lifetime_value"].astype("Float64")).to_numpy()
        df.loc[buyers, "rfm_label"] = [rfm_label(r, f, m) for r, f, m in
                                       zip(df.loc[buyers, "rfm_r"], df.loc[buyers, "rfm_f"], df.loc[buyers, "rfm_m"])]
    return df, as_of


# ----------------------------------------------------------------- one function per question
# Every function: (enriched customer_360, config) -> a small DataFrame. Each becomes
# output/insights/<name>.csv and a chart or table on the dashboard.

PROFILE = ["customer_id", "name", "city"]   # no email in reports - privacy (D-17)


def top_customers(df, config):
    """REQUIRED: Top 10 customers by revenue (lifetime_value), highest first."""
    n = config["insights"]["top_n"]
    cols = PROFILE + ["segment", "total_orders", "lifetime_value", "avg_order_value"]
    top = df.sort_values(["lifetime_value", "customer_id"], ascending=[False, True]).head(n)[cols]
    top.insert(0, "rank", range(1, len(top) + 1))
    return top.reset_index(drop=True)


def segment_summary(df, config):
    """REQUIRED: Gold / Silver / Bronze - how many customers and how much revenue each."""
    order = ["Gold", "Silver", "Bronze", NO_ORDER_DATA]
    g = df.groupby("segment").agg(customers=("customer_id", "count"),
                                  revenue=("lifetime_value", "sum"),
                                  avg_revenue=("lifetime_value", "mean"),
                                  avg_tickets=("support_tickets", "mean"))
    g = g.reindex([s for s in order if s in g.index]).reset_index()
    g["customer_share_pct"] = (100 * g["customers"] / g["customers"].sum()).round(1)
    g["revenue_share_pct"] = (100 * g["revenue"] / max(g["revenue"].sum(), 1)).round(1)
    return g.round(2)


def high_ticket_customers(df, config):
    """REQUIRED: customers with MORE than 5 support tickets, most tickets first."""
    cols = PROFILE + ["segment", "support_tickets", "last_ticket_date", "lifetime_value"]
    out = df[df["high_ticket_customer"]].sort_values(["support_tickets", "customer_id"], ascending=[False, True])
    return out[cols].reset_index(drop=True)


def revenue_by_city(df, config):
    """REQUIRED: total revenue per city, highest first."""
    g = df.groupby("city", as_index=False)["lifetime_value"].sum().rename(columns={"lifetime_value": "revenue"})
    return g.sort_values("revenue", ascending=False).reset_index(drop=True)


def customers_by_city(df, config):
    """REQUIRED: number of customers per city, highest first."""
    g = df.groupby("city", as_index=False)["customer_id"].count().rename(columns={"customer_id": "customers"})
    return g.sort_values("customers", ascending=False).reset_index(drop=True)


def city_scorecard(df, config):
    """EXTRA: everything about a city in one row - size, money, and support load."""
    g = df.groupby("city").agg(customers=("customer_id", "count"),
                               revenue=("lifetime_value", "sum"),
                               gold_customers=("segment", lambda s: (s == "Gold").sum()),
                               tickets=("support_tickets", "sum"),
                               high_ticket_customers=("high_ticket_customer", "sum"))
    g["revenue_per_customer"] = (g["revenue"] / g["customers"]).round(0)
    g["tickets_per_customer"] = (g["tickets"] / g["customers"]).round(2)
    return g.sort_values("revenue", ascending=False).reset_index()


def at_risk_vips(df, config):
    """EXTRA: Gold/Silver customers with no order for 90+ days - a win-back call list."""
    days = config["insights"]["at_risk_days"]
    risky = df[df["segment"].isin(["Gold", "Silver"]) & (df["days_since_last_order"] >= days).fillna(False)]
    cols = PROFILE + ["segment", "lifetime_value", "last_order_date", "days_since_last_order"]
    return risky.sort_values(["lifetime_value", "customer_id"], ascending=[False, True])[cols].reset_index(drop=True)


def unhappy_high_value(df, config):
    """EXTRA: Gold customers with more than 5 tickets - escalate to support leads first."""
    out = df[(df["segment"] == "Gold") & df["high_ticket_customer"]]
    cols = PROFILE + ["lifetime_value", "support_tickets", "last_ticket_date"]
    return out.sort_values(["support_tickets", "lifetime_value"], ascending=False)[cols].reset_index(drop=True)


def rfm_summary(df, config):
    """EXTRA: how many customers in each RFM group, their revenue, and what to do."""
    g = df.groupby("rfm_label").agg(customers=("customer_id", "count"),
                                    revenue=("lifetime_value", "sum"),
                                    avg_days_since_order=("days_since_last_order", "mean"))
    g = g.reset_index().round(1)
    g["action"] = g["rfm_label"].map(RFM_ACTIONS).fillna("Collect order data first")
    return g.sort_values("revenue", ascending=False).reset_index(drop=True)


def pareto_curve(df, config):
    """EXTRA: cumulative revenue share as we add customers from biggest to smallest.

    Used to answer: "Do a few customers bring most of the money?" (the 80/20 rule).
    """
    revenue = df["lifetime_value"].astype(float).sort_values(ascending=False).reset_index(drop=True)
    total = revenue.sum()
    n = len(revenue)
    return pd.DataFrame({
        "customer_share_pct": (100 * (revenue.index + 1) / max(n, 1)).round(2),
        "revenue_share_pct": (100 * revenue.cumsum() / total).round(2) if total else 0.0,
    })


def support_vs_value(df, config):
    """EXTRA: do our best customers complain more? Average tickets per segment."""
    g = df.groupby("segment").agg(customers=("customer_id", "count"),
                                  avg_tickets=("support_tickets", "mean"),
                                  pct_high_ticket=("high_ticket_customer", "mean"),
                                  avg_revenue=("lifetime_value", "mean"))
    g["pct_high_ticket"] = (100 * g["pct_high_ticket"]).round(1)
    return g.round(2).reset_index()


def registration_cohorts(df, config):
    """EXTRA: customers and revenue grouped by the month they registered."""
    month = df["registration_date"].dt.to_period("M").astype(str)
    g = df.assign(registration_month=month).groupby("registration_month").agg(
        customers=("customer_id", "count"), revenue=("lifetime_value", "sum"),
        avg_revenue=("lifetime_value", "mean"))
    return g.round(0).reset_index()


# Name -> function. Each becomes output/insights/<name>.csv.
# >>> TO CHANGE: a new insight / KPI = write one function above and add one line here.
INSIGHTS = {
    "top_customers": top_customers,
    "segment_summary": segment_summary,
    "high_ticket_customers": high_ticket_customers,
    "revenue_by_city": revenue_by_city,
    "customers_by_city": customers_by_city,
    "city_scorecard": city_scorecard,
    "at_risk_vips": at_risk_vips,
    "unhappy_high_value": unhappy_high_value,
    "rfm_summary": rfm_summary,
    "pareto_curve": pareto_curve,
    "support_vs_value": support_vs_value,
    "registration_cohorts": registration_cohorts,
}


def pareto_share(df, config):
    """% of revenue that comes from the top 20% of customers (share set in config)."""
    revenue = df["lifetime_value"].astype(float).sort_values(ascending=False)
    if len(revenue) == 0 or revenue.sum() == 0:
        return 0.0
    top_n = max(1, math.ceil(len(revenue) * config["insights"]["pareto_top_share"]))
    return round(100 * revenue.head(top_n).sum() / revenue.sum(), 1)


def headlines(df, tables, config):
    """Plain-English findings, COMPUTED from the data (so they stay true if data changes)."""
    if df.empty:
        return ["No customers to analyse."]
    lines = []
    share = config["insights"]["pareto_top_share"]
    lines.append(f"The top {share:.0%} of customers bring {pareto_share(df, config)}% of all revenue.")

    seg = tables["segment_summary"].set_index("segment")
    if "Gold" in seg.index:
        lines.append(f"Gold customers are {seg.loc['Gold', 'customer_share_pct']}% of customers "
                     f"but bring {seg.loc['Gold', 'revenue_share_pct']}% of revenue.")

    city = tables["revenue_by_city"].iloc[0]
    lines.append(f"{city['city']} is the top city by revenue ({format_inr(city['revenue'])}).")

    high = int(df["high_ticket_customer"].sum())
    lines.append(f"{high} customers ({100 * high / len(df):.0f}%) raised more than "
                 f"{config['insights']['high_ticket_threshold']} support tickets.")

    tickets, value = df["support_tickets"].astype(float), df["lifetime_value"].astype(float)
    # Correlation needs variation in both columns (if everyone has 4 tickets there is nothing to compare).
    r = tickets.corr(value) if tickets.std() > 0 and value.std() > 0 else None
    if r is not None and pd.notna(r):
        strength = "no real link" if abs(r) < 0.1 else "a weak link" if abs(r) < 0.3 else "a clear link"
        lines.append(f"Tickets vs revenue correlation is r = {r:.2f}: {strength} between spending and complaining.")

    risk = tables["at_risk_vips"]
    if len(risk):
        lines.append(f"{len(risk)} Gold/Silver customers have not ordered for "
                     f"{config['insights']['at_risk_days']}+ days: {format_inr(risk['lifetime_value'].sum())} "
                     f"of lifetime value at risk.")
    return lines


def build_insights(df, config):
    """STEP 5: run every insight function. Returns ({name: table}, [headline sentences])."""
    tables = {name: fn(df, config) for name, fn in INSIGHTS.items()}
    lines = headlines(df, tables, config)
    log.info("  insights  built %d tables", len(tables))
    for line in lines:
        log.info("    * %s", line)
    return tables, lines
