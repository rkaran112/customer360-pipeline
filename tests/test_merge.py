"""STEP 4 tests - the Customer 360 join: one row per customer, nobody lost, no money invented."""
from pipeline import extract, merge, quality, standardize
from tests.conftest import GOOD_CRM, write_sources

BRIEF_COLUMNS = ["customer_id", "name", "email", "city", "registration_date", "total_orders",
                 "lifetime_value", "last_order_date", "support_tickets", "last_ticket_date"]


def run_step4(config, **files):
    write_sources(config["input_dir"], **files)
    tables, _ = standardize.standardize_all(extract.extract_all(config), config)
    clean, _, _ = quality.check_all(tables, config)
    return clean, merge.build_customer_360(clean)


def test_one_row_per_customer(config):
    """Each customer_id appears once. Why: a duplicate row would double that customer's revenue."""
    _, c360 = run_step4(config)
    assert c360["customer_id"].is_unique and len(c360) == 3


def test_customer_without_orders_is_kept_and_flagged(config):
    """No orders -> still in the 360, has_order_data=False, orders 0. Why: an INNER join would silently drop them."""
    orders = "cust_id,total_orders,lifetime_value,last_order_date\nC0001,15,125000,2026-08-10\n"
    _, c360 = run_step4(config, orders=orders)
    row = c360.set_index("customer_id").loc["C0002"]
    assert len(c360) == 3 and not row["has_order_data"] and row["total_orders"] == 0


def test_customer_without_tickets_is_kept(config):
    """No tickets -> kept with 0 tickets and has_ticket_data=False. Why: no complaints is normal, not an error."""
    tickets = "<customers><customer><customerId>C0001</customerId><tickets>2</tickets><lastTicketDate>2026-01-01</lastTicketDate></customer></customers>"
    _, c360 = run_step4(config, tickets=tickets)
    row = c360.set_index("customer_id").loc["C0003"]
    assert row["support_tickets"] == 0 and not row["has_ticket_data"]


def test_orphan_orders_are_counted_not_merged(config):
    """An order for unknown customer C0099 is counted as an orphan. Why: it must be visible, not hidden or merged."""
    orders = GOOD_CRM and "cust_id,total_orders,lifetime_value,last_order_date\nC0001,1,100,2026-01-01\nC0099,1,500,2026-01-01\n"
    clean, c360 = run_step4(config, orders=orders)
    integrity = merge.referential_integrity(clean, c360)
    assert integrity["orders_without_customer"] == 1
    assert "C0099" not in set(c360["customer_id"])


def test_output_has_the_brief_columns(config):
    """The 360 has every column of the brief's expected dataset, in that order. Why: it is the output contract."""
    _, c360 = run_step4(config)
    assert list(c360.columns[:len(BRIEF_COLUMNS)]) == BRIEF_COLUMNS


def test_brief_example_record_matches(config):
    """C0001 from the brief merges to exactly the brief's example values. Why: proves the merge end to end on known data."""
    _, c360 = run_step4(config)
    c = c360.set_index("customer_id").loc["C0001"]
    assert (c["name"], c["total_orders"], c["lifetime_value"], c["support_tickets"]) == ("Amit Sharma", 15, 125000, 4)


def test_revenue_is_conserved(config):
    """Sum of revenue in the 360 = sum of clean orders for known customers. Why: a join must never create or lose money."""
    clean, c360 = run_step4(config)
    assert c360["lifetime_value"].sum() == clean["orders"]["lifetime_value"].sum() == 380000
