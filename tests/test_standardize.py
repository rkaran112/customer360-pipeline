"""STEP 2 tests - one schema, tidy values, real types, and the schema drift guard."""
import pandas as pd
import pytest

from pipeline import extract, standardize
from tests.conftest import GOOD_CRM, source_cfg, write_sources


def run_step2(config, **files):
    write_sources(config["input_dir"], **files)
    raw = extract.extract_all(config)
    return standardize.standardize_all(raw, config)


def test_different_id_names_become_customer_id(config):
    """cust_id (CSV) and customerId (XML) both become customer_id. Why: the merge needs one shared key name."""
    tables, _ = run_step2(config)
    for name in ["crm", "orders", "tickets"]:
        assert "customer_id" in tables[name].columns
    assert "cust_id" not in tables["orders"].columns and "customerId" not in tables["tickets"].columns
    assert "support_tickets" in tables["tickets"].columns


def test_email_lowercased_city_titlecased_spaces_trimmed(config):
    """'  AMIT@GMAIL.COM ' -> 'amit@gmail.com', ' mumbai ' -> 'Mumbai'. Why: otherwise 'mumbai' and 'Mumbai' split city totals."""
    crm = [dict(GOOD_CRM[0], email="  AMIT@GMAIL.COM ", city=" mumbai ", name="Amit   Sharma")]
    tables, _ = run_step2(config, crm=crm)
    row = tables["crm"].iloc[0]
    assert row["email"] == "amit@gmail.com" and row["city"] == "Mumbai" and row["name"] == "Amit Sharma"


def test_bad_numbers_become_missing_not_crash(config):
    """'abc' or '2.5' orders -> missing (step 3 then reports it). Why: one bad value must not stop the whole load."""
    orders = "cust_id,total_orders,lifetime_value,last_order_date\nC0001,abc,1000,2026-01-01\nC0002,2.5,N/A,2026-01-01\n"
    tables, _ = run_step2(config, orders=orders)
    o = tables["orders"]
    assert o["total_orders"].isna().all()
    assert pd.isna(o.loc[1, "lifetime_value"]) and o.loc[0, "lifetime_value"] == 1000


def test_dates_parse_and_invalid_dates_become_missing(config):
    """'2024-01-05' becomes a real date; '2026-13-45' becomes missing. Why: date maths (days since order) needs real dates."""
    crm = [dict(GOOD_CRM[0], registration_date="2026-13-45"), GOOD_CRM[1]]
    tables, _ = run_step2(config, crm=crm)
    dates = tables["crm"]["registration_date"]
    assert pd.isna(dates.iloc[0]) and dates.iloc[1] == pd.Timestamp("2024-01-12")


def test_extra_column_gives_warning_and_continues(config):
    """An unknown new column -> a warning, the run continues. Why: harmless changes should not stop the business."""
    crm = [dict(c, loyalty_tier="X") for c in GOOD_CRM]
    tables, warnings = run_step2(config, crm=crm)
    assert any("loyalty_tier" in w for w in warnings)
    assert "loyalty_tier" not in tables["crm"].columns and len(tables["crm"]) == 3


def test_missing_mandatory_column_stops_the_run(config):
    """lifetime_value column missing -> SchemaDriftError. Why: publishing revenue without revenue data would be wrong."""
    orders = "cust_id,total_orders,last_order_date\nC0001,15,2026-08-10\n"
    with pytest.raises(standardize.SchemaDriftError, match="lifetime_value"):
        run_step2(config, orders=orders)


def test_standardize_never_removes_rows(config):
    """Step 2 only cleans; row counts stay the same. Why: removing rows is step 3's job, where it is counted."""
    write_sources(config["input_dir"])
    raw = extract.extract_all(config)
    tables, _ = standardize.standardize_all(raw, config)
    assert {k: len(v) for k, v in tables.items()} == {k: len(v) for k, v in raw.items()}
    assert source_cfg(config, "orders")["column_mapping"]["cust_id"] == "customer_id"
