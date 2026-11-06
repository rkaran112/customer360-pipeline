"""STEP 1 tests - can we read every format, and do bad inputs fail clearly?"""
import pytest

from pipeline import extract
from tests.conftest import source_cfg, write_sources


def test_json_loads_all_records_and_columns(config):
    """JSON is read with the right number of rows and columns. Why: the CRM is the master customer list."""
    write_sources(config["input_dir"])
    df = extract.extract_source(source_cfg(config, "crm"), config["input_dir"])
    assert len(df) == 3
    assert {"customer_id", "name", "email", "city", "registration_date"} <= set(df.columns)


def test_csv_keeps_every_value_as_text(config):
    """CSV values stay text ('15' not 15). Why: a bad value like 'N/A' must not crash the load."""
    write_sources(config["input_dir"], orders="cust_id,total_orders,lifetime_value,last_order_date\nC0001,N/A,0012,2026-01-01\n")
    df = extract.extract_source(source_cfg(config, "orders"), config["input_dir"])
    assert df.loc[0, "total_orders"] == "N/A"
    assert df.loc[0, "lifetime_value"] == "0012"     # leading zero kept: no silent type guessing


def test_xml_empty_tag_becomes_empty_text(config):
    """<customerId /> becomes '' instead of crashing. Why: this exact defect is in the real file (record 450)."""
    xml = ("<customers><customer><customerId /><tickets>3</tickets><lastTicketDate>2026-01-01</lastTicketDate>"
           "</customer></customers>")
    write_sources(config["input_dir"], tickets=xml)
    df = extract.extract_source(source_cfg(config, "tickets"), config["input_dir"])
    assert df.loc[0, "customerId"] == "" and df.loc[0, "tickets"] == "3"


def test_unknown_format_raises_clear_error(config):
    """A typo like 'jsn' in config gives a clear message. Why: config errors should be obvious, not cryptic."""
    write_sources(config["input_dir"])
    bad = dict(source_cfg(config, "crm"), format="jsn")
    with pytest.raises(ValueError, match="unknown format 'jsn'"):
        extract.extract_source(bad, config["input_dir"])


def test_missing_file_names_the_file(config):
    """A missing input file stops the run and names the file. Why: the on-call person knows what to fix."""
    config["input_dir"].mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="orders.csv"):
        extract.extract_source(source_cfg(config, "orders"), config["input_dir"])


@pytest.mark.parametrize("name, content", [
    ("crm", "[]"),
    ("orders", "cust_id,total_orders,lifetime_value,last_order_date\n"),
    ("tickets", "<customers/>"),
])
def test_empty_file_gives_zero_rows_not_crash(config, name, content):
    """An empty file (a quiet day) loads as 0 rows with the expected columns. Why: empty days happen."""
    write_sources(config["input_dir"], **{name: content if name != "crm" else []})
    if name == "crm":
        (config["input_dir"] / "crm_customers.json").write_text(content)
    df = extract.extract_source(source_cfg(config, name), config["input_dir"])
    assert len(df) == 0
    assert set(source_cfg(config, name)["expected_columns"]) <= set(df.columns)


def test_tracking_columns_added(config):
    """Every row gets _source and _source_row (1, 2, 3...). Why: lineage and quarantine point back to the exact record."""
    write_sources(config["input_dir"])
    tables = extract.extract_all(config)
    for name, df in tables.items():
        assert (df["_source"] == name).all()
        assert list(df["_source_row"]) == [1, 2, 3]
