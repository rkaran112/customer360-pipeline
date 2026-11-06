"""STEP 3 tests - each data-quality rule, rule order, quarantine, DQ report and reconciliation."""
import pytest

from pipeline import extract, quality, standardize
from tests.conftest import GOOD_CRM, write_sources


def run_step3(config, **files):
    write_sources(config["input_dir"], **files)
    raw = extract.extract_all(config)
    tables, drift = standardize.standardize_all(raw, config)
    clean, quarantine, lineage = quality.check_all(tables, config)
    report = quality.build_dq_report(tables, clean, quarantine, drift)
    return clean, quarantine, report


def test_duplicate_keeps_first_and_quarantines_second(config):
    """Same customer twice -> first kept, second quarantined as 'duplicates'. Why: duplicates double-count customers."""
    crm = GOOD_CRM + [dict(GOOD_CRM[0], name="Amit Copy")]
    clean, quarantine, report = run_step3(config, crm=crm)
    assert report["duplicates"] == 1
    assert clean["crm"].set_index("customer_id").loc["C0001", "name"] == "Amit Sharma"
    assert quarantine.iloc[0]["reason"] == "duplicates" and quarantine.iloc[0]["_source_row"] == 4


@pytest.mark.parametrize("email", ["user.com", "a@b", "@x.com", "a b@x.com", "user125example.com"])
def test_invalid_emails_are_rejected(config, email):
    """Emails without @, domain or with spaces are rejected. Why: marketing and support cannot reach these customers."""
    crm = [dict(GOOD_CRM[0], email=email)] + GOOD_CRM[1:]
    _, quarantine, report = run_step3(config, crm=crm)
    assert report["invalid_emails"] == 1 and quarantine.iloc[0]["customer_id"] == "C0001"


@pytest.mark.parametrize("email", ["a.b+1@x.co.in", "UPPER@Example.COM", "first_last@mail-server.org"])
def test_valid_emails_pass(config, email):
    """Real-world valid emails (plus sign, sub-domains, capitals) pass. Why: a too-strict rule would reject good customers."""
    crm = [dict(GOOD_CRM[0], email=email)] + GOOD_CRM[1:]
    _, _, report = run_step3(config, crm=crm)
    assert report["invalid_emails"] == 0


def test_missing_mandatory_field_is_rejected(config):
    """Blank total_orders -> 'missing_fields' with the column named. Why: the brief's 'missing values' challenge."""
    orders = "cust_id,total_orders,lifetime_value,last_order_date\nC0001,,125000,2026-08-10\nC0002,7,35000,2026-08-02\n"
    _, quarantine, report = run_step3(config, orders=orders)
    assert report["missing_fields"] == 1
    assert quarantine.iloc[0]["detail"] == "missing: total_orders"


def test_negative_revenue_rejected_but_zero_allowed(config):
    """-5000 is rejected; 0 is allowed (a customer who has not paid yet). Why: negative lifetime value is impossible."""
    orders = "cust_id,total_orders,lifetime_value,last_order_date\nC0001,1,-5000,2026-08-10\nC0002,0,0,2026-08-02\n"
    clean, _, report = run_step3(config, orders=orders)
    assert report["negative_revenue"] == 1
    assert list(clean["orders"]["customer_id"]) == ["C0002"]


@pytest.mark.parametrize("bad_id", ["", "C12", "X0001", "c0001", "C00001"])
def test_invalid_customer_ids_are_rejected(config, bad_id):
    """Blank or wrongly shaped ids are rejected. Why: a record with a wrong id can never be joined to its customer."""
    orders = f"cust_id,total_orders,lifetime_value,last_order_date\n{bad_id},1,100,2026-08-10\n"
    _, _, report = run_step3(config, orders=orders)
    assert report["invalid_customer_ids"] == 1


def test_id_with_spaces_is_trimmed_and_passes(config):
    """' C0001 ' is trimmed to 'C0001' and passes. Why: stray spaces are a formatting slip, not a wrong id."""
    orders = "cust_id,total_orders,lifetime_value,last_order_date\n C0001 ,1,100,2026-08-10\n"
    clean, _, report = run_step3(config, orders=orders)
    assert report["invalid_customer_ids"] == 0 and clean["orders"].iloc[0]["customer_id"] == "C0001"


def test_first_broken_rule_wins_and_counts_once(config):
    """A row with a bad id AND a bad email counts once, as an invalid id. Why: the report must add up exactly."""
    crm = [dict(GOOD_CRM[0], customer_id="BAD", email="no-at-sign")] + GOOD_CRM[1:]
    _, quarantine, report = run_step3(config, crm=crm)
    assert report["invalid_customer_ids"] == 1 and report["invalid_emails"] == 0
    assert len(quarantine) == 1


def test_two_blank_ids_are_not_also_duplicates(config):
    """Two blank ids = 2 invalid ids, not 1 invalid + 1 duplicate. Why: shows the rule order is applied correctly."""
    orders = "cust_id,total_orders,lifetime_value,last_order_date\n,1,100,2026-08-10\n,2,200,2026-08-10\n"
    _, _, report = run_step3(config, orders=orders)
    assert report["invalid_customer_ids"] == 2 and report["duplicates"] == 0


def test_every_quarantined_row_has_reason_source_and_record(config):
    """Each quarantined row says why, from which source and which record. Why: the source team must be able to fix it."""
    crm = GOOD_CRM + [GOOD_CRM[0], dict(GOOD_CRM[1], customer_id="C0009", email="bad")]
    _, quarantine, _ = run_step3(config, crm=crm)
    assert len(quarantine) == 2
    assert quarantine[["reason", "detail", "_source", "_source_row"]].notna().all().all()


def test_clean_and_quarantine_split_every_row_exactly_once(config):
    """No row is in both clean and quarantine, and none is lost. Why: this is the core 'nothing lost' guarantee."""
    crm = GOOD_CRM + [GOOD_CRM[0], dict(GOOD_CRM[1], customer_id="C0009", email="bad")]
    clean, quarantine, _ = run_step3(config, crm=crm)
    clean_rows = set(clean["crm"]["_source_row"])
    bad_rows = set(quarantine[quarantine["_source"] == "crm"]["_source_row"])
    assert clean_rows.isdisjoint(bad_rows) and clean_rows | bad_rows == {1, 2, 3, 4, 5}


def test_report_has_every_key_from_the_brief(config):
    """dq_report.json has the 5 keys from the brief's example. Why: the brief defines this output contract."""
    _, _, report = run_step3(config)
    for key in ["total_records", "duplicates", "invalid_emails", "missing_fields", "successful_records"]:
        assert key in report


def test_report_reconciles(config):
    """total = sum of every failure type + successful. Why: proves no record vanished between steps."""
    crm = GOOD_CRM + [GOOD_CRM[0]]
    _, _, report = run_step3(config, crm=crm)
    assert report["reconciled"] is True
    assert report["total_records"] == 10 and report["successful_records"] == 9


def test_reconcile_detects_a_broken_count(config):
    """If a count is wrong, reconcile() returns False. Why: the safety check itself must be able to fail."""
    _, _, report = run_step3(config)
    report["successful_records"] -= 1
    assert quality.reconcile(report) is False


def test_dq_score_per_source(config):
    """DQ score = passed / total * 100 per source, always 0-100. Why: shows which upstream system is the dirtiest."""
    crm = GOOD_CRM + [GOOD_CRM[0]]           # 3 of 4 pass
    _, _, report = run_step3(config, crm=crm)
    assert report["by_source"]["crm"]["dq_score_pct"] == 75.0
    assert report["by_source"]["orders"]["dq_score_pct"] == 100.0
    assert all(0 <= s["dq_score_pct"] <= 100 for s in report["by_source"].values())
