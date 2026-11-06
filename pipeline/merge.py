"""
STEP 4 - MERGE: build the Customer 360 dataset (one row per customer).

Input : the clean tables from step 3 (crm, orders, tickets)
Output: customer_360 table - one row per customer with profile + orders + support,
        exactly the "Expected Unified Customer Dataset" from the brief, plus 2 flags.

How the join works (decision D-06):
    CRM is the MASTER list of customers (it is the only source with name/email/city).
        customer_360 = CRM  LEFT JOIN orders  LEFT JOIN tickets   (on customer_id)
    LEFT JOIN = keep every CRM customer, even if they have no orders or tickets yet.
    An INNER join would silently drop those customers - a classic data bug.

    Picture it:   CRM (497)  --left join-->  orders (498)  --left join-->  tickets (499)
                  every CRM customer stays; orders/tickets attach where the id matches.
"""
import pandas as pd

from pipeline.logger import get_logger

log = get_logger()

# Columns taken from each source, in the order they appear in the final dataset.
# >>> TO CHANGE: to add a column from a source to the 360, add it to the right list here
#     (and to that source's "expected_columns" in config.json).
CRM_COLUMNS = ["customer_id", "name", "email", "city", "registration_date"]
ORDER_COLUMNS = ["customer_id", "total_orders", "lifetime_value", "last_order_date"]
TICKET_COLUMNS = ["customer_id", "support_tickets", "last_ticket_date"]


def build_customer_360(clean_tables):
    """Join the 3 clean sources into one row per customer."""
    crm = clean_tables["crm"][CRM_COLUMNS]
    orders = clean_tables["orders"][ORDER_COLUMNS]
    tickets = clean_tables["tickets"][TICKET_COLUMNS]

    # WHAT: the two LEFT JOINs.
    # WHY validate="one_to_one": pandas raises an error if an id appears twice on either side.
    #     A duplicate would silently multiply rows (and revenue!). Step 3 removes duplicates,
    #     and this line is the safety net that proves it.
    c360 = (crm
            .merge(orders, on="customer_id", how="left", validate="one_to_one")
            .merge(tickets, on="customer_id", how="left", validate="one_to_one"))

    # WHAT: flags saying whether we actually HAVE order / ticket data for this customer.
    # WHY (D-07): "0 orders because we have no data" is different from "0 orders".
    #     The flag keeps that difference visible after we fill the gaps with 0 below.
    c360["has_order_data"] = c360["lifetime_value"].notna()
    c360["has_ticket_data"] = c360["support_tickets"].notna()

    # WHAT: customers with no orders/tickets get 0 instead of empty, so totals and charts work.
    c360["total_orders"] = c360["total_orders"].fillna(0)
    c360["lifetime_value"] = c360["lifetime_value"].fillna(0)
    c360["support_tickets"] = c360["support_tickets"].fillna(0)

    log.info("  merged    %d CRM customers -> %d rows in customer_360 (%d without order data, %d without ticket data)",
             len(crm), len(c360), (~c360["has_order_data"]).sum(), (~c360["has_ticket_data"]).sum())
    return c360.sort_values("customer_id").reset_index(drop=True)


def referential_integrity(clean_tables, c360):
    """Count records that could NOT be attached to a customer (orphans).

    Example: C0125's CRM record was quarantined (bad email), so their clean order and
    ticket records have no customer to attach to. They are counted here, never hidden.
    """
    known = set(c360["customer_id"])
    return {
        "orders_without_customer": int((~clean_tables["orders"]["customer_id"].isin(known)).sum()),
        "tickets_without_customer": int((~clean_tables["tickets"]["customer_id"].isin(known)).sum()),
        "customers_without_orders": int((~c360["has_order_data"]).sum()),
        "customers_without_tickets": int((~c360["has_ticket_data"]).sum()),
    }


def finish_lineage(lineage, c360):
    """Add the final outcome of each PASSED record: merged into the 360, or orphan."""
    known = set(c360["customer_id"])

    def outcome(row):
        if row["status"] == "QUARANTINED":
            return "QUARANTINED"
        return "MERGED" if row["customer_id"] in known else "ORPHAN (customer record not in CRM clean data)"
    lineage = lineage.copy()
    lineage["status"] = lineage.apply(outcome, axis=1) if len(lineage) else lineage["status"]
    return lineage
