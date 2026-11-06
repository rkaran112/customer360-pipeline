"""STEP 5 tests - segments, top 10, tickets, city totals, RFM, Pareto and dates."""
import pandas as pd
import pytest

from pipeline import insights
from pipeline.privacy import mask_email


def make_360(values, tickets=None, days=None):
    """A small fake customer_360 with the given lifetime values."""
    n = len(values)
    tickets = tickets or [1] * n
    last_order = [pd.Timestamp("2026-10-01") - pd.Timedelta(days=d) for d in (days or [10] * n)]
    return pd.DataFrame({
        "customer_id": [f"C{i:04d}" for i in range(1, n + 1)],
        "name": [f"Name {i}" for i in range(1, n + 1)],
        "email": [f"u{i}@x.com" for i in range(1, n + 1)],
        "city": (["Mumbai", "Delhi"] * n)[:n],
        "registration_date": pd.to_datetime(["2024-01-05"] * n),
        "total_orders": pd.array([5] * n, dtype="Int64"),
        "lifetime_value": pd.array(values, dtype="Float64"),
        "last_order_date": pd.to_datetime(last_order),
        "support_tickets": pd.array(tickets, dtype="Int64"),
        "last_ticket_date": pd.to_datetime(["2026-10-01"] * n),
        "has_order_data": [True] * n,
        "has_ticket_data": [True] * n,
    })


@pytest.mark.parametrize("revenue, expected", [
    (100001, "Gold"), (100000, "Silver"), (50000, "Silver"), (49999, "Bronze"), (0, "Bronze")])
def test_segment_boundaries(config, revenue, expected):
    """Exactly 100000 is Silver, 100001 is Gold, 49999 is Bronze. Why: off-by-one at boundaries is the most common business-rule bug."""
    df, _ = insights.enrich(make_360([revenue]), config)
    assert df.loc[0, "segment"] == expected


def test_no_order_data_segment(config):
    """A customer without order data is 'No order data', not Bronze. Why: we must not guess they are low value."""
    df = make_360([0])
    df["has_order_data"] = False
    out, _ = insights.enrich(df, config)
    assert out.loc[0, "segment"] == insights.NO_ORDER_DATA


def test_top_customers_sorted_and_limited(config):
    """Top 10 is highest first and has at most 10 rows; works with fewer customers. Why: the brief's revenue ranking."""
    df, _ = insights.enrich(make_360(list(range(1000, 16000, 1000))), config)
    top = insights.top_customers(df, config)
    assert len(top) == 10 and top["lifetime_value"].is_monotonic_decreasing and top.iloc[0]["lifetime_value"] == 15000
    few, _ = insights.enrich(make_360([5, 7]), config)
    assert len(insights.top_customers(few, config)) == 2


def test_more_than_5_tickets_is_strict(config):
    """5 tickets is NOT included, 6 is. Why: the brief says 'more than 5'."""
    df, _ = insights.enrich(make_360([1, 2, 3], tickets=[5, 6, 12]), config)
    assert list(insights.high_ticket_customers(df, config)["support_tickets"]) == [12, 6]


def test_city_totals_add_up(config):
    """Revenue by city sums to total revenue; counts sum to customers. Why: regional reports must match the total."""
    df, _ = insights.enrich(make_360([100, 200, 300, 400, 500]), config)
    assert insights.revenue_by_city(df, config)["revenue"].sum() == 1500
    assert insights.customers_by_city(df, config)["customers"].sum() == 5


def test_rfm_scores_and_one_label_each(config):
    """RFM scores are 1-3 and every customer gets exactly one known label. Why: an unlabelled customer gets no action."""
    df, _ = insights.enrich(make_360(list(range(10, 200, 10)), days=list(range(5, 400, 21))), config)
    for col in ["rfm_r", "rfm_f", "rfm_m"]:
        assert df[col].between(1, 3).all()
    assert df["rfm_label"].isin(insights.RFM_ACTIONS).all()


def test_pareto_share_bounds(config):
    """Top-20% share is between 0 and 100, and 100 with a single customer. Why: a percentage outside 0-100 is a bug."""
    df, _ = insights.enrich(make_360([10, 20, 30, 40, 1000]), config)
    assert insights.pareto_share(df, config) == pytest.approx(1000 / 1100 * 100, abs=0.1)
    one, _ = insights.enrich(make_360([500]), config)
    assert insights.pareto_share(one, config) == 100.0


def test_as_of_date_comes_from_config_not_the_clock(config):
    """days_since_last_order uses config as_of_date. Why: the same input must give the same output every day (idempotent)."""
    config["as_of_date"] = "2026-10-11"
    df, as_of = insights.enrich(make_360([100], days=[10]), config)      # last order 2026-09-21
    assert as_of == pd.Timestamp("2026-10-11") and df.loc[0, "days_since_last_order"] == 20


def test_at_risk_vips_use_the_day_threshold(config):
    """Gold/Silver with 90+ days since order are at risk; recent or Bronze are not. Why: this is a call list - no false alarms."""
    config["as_of_date"] = "2026-10-01"
    df, _ = insights.enrich(make_360([200000, 200000, 20000], days=[120, 10, 300]), config)
    assert list(insights.at_risk_vips(df, config)["customer_id"]) == ["C0001"]


def test_average_order_value_never_divides_by_zero(config):
    """0 orders -> empty average order value, not infinity or a crash. Why: new customers have 0 orders."""
    df = make_360([0, 1000])
    df["total_orders"] = pd.array([0, 4], dtype="Int64")
    out, _ = insights.enrich(df, config)
    assert pd.isna(out.loc[0, "avg_order_value"]) and out.loc[1, "avg_order_value"] == 250


def test_headlines_are_computed_from_data(config):
    """Headline sentences contain numbers from the data. Why: hand-typed findings go stale when the data changes."""
    df, _ = insights.enrich(make_360([100000, 200000, 300000]), config)
    tables, lines = insights.build_insights(df, config)
    assert any("Mumbai" in line or "Delhi" in line for line in lines)
    assert set(tables) == set(insights.INSIGHTS)


@pytest.mark.parametrize("email, masked", [
    ("amit.sharma@gmail.com", "am***@gmail.com"), ("no-at-sign", "***"), (None, "***")])
def test_email_masking(email, masked):
    """Emails are masked on screens. Why: the dashboard is seen by many people; emails are personal data."""
    assert mask_email(email) == masked


def test_inr_formatting():
    """Indian number words: lakh and crore. Why: the business reads money in lakh/crore."""
    assert insights.format_inr(125000) == "Rs 1.25 lakh"
    assert insights.format_inr(19400000) == "Rs 1.94 crore"
    assert insights.format_inr(999) == "Rs 999"
