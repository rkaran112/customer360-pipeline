"""
Shared test helpers (pytest loads this file automatically).

Most tests use TINY hand-made files written into a temporary folder (tmp_path), so:
  - a test never depends on an earlier pipeline run,
  - each test shows exactly the defect it is about,
  - tests never touch the real data/ or output/ folders.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # lets tests "import pipeline" and "import run_pipeline"

from pipeline.config import load_config  # noqa: E402
from pipeline.logger import setup_logging  # noqa: E402

GOOD_CRM = [
    {"customer_id": "C0001", "name": "Amit Sharma", "email": "amit.sharma@gmail.com", "city": "Mumbai",
     "registration_date": "2024-01-05"},
    {"customer_id": "C0002", "name": "Priya Nair", "email": "priya.nair@yahoo.com", "city": "Bengaluru",
     "registration_date": "2024-01-12"},
    {"customer_id": "C0003", "name": "Rahul Verma", "email": "rahul.verma@gmail.com", "city": "Delhi",
     "registration_date": "2024-02-01"},
]
GOOD_ORDERS = ("cust_id,total_orders,lifetime_value,last_order_date\n"
               "C0001,15,125000,2026-08-10\nC0002,7,35000,2026-08-02\nC0003,22,220000,2026-08-11\n")
GOOD_TICKETS = ("<customers>"
                "<customer><customerId>C0001</customerId><tickets>4</tickets><lastTicketDate>2026-07-15</lastTicketDate></customer>"
                "<customer><customerId>C0002</customerId><tickets>1</tickets><lastTicketDate>2026-06-22</lastTicketDate></customer>"
                "<customer><customerId>C0003</customerId><tickets>8</tickets><lastTicketDate>2026-08-01</lastTicketDate></customer>"
                "</customers>")


def write_sources(folder, crm=None, orders=None, tickets=None):
    """Write the 3 source files into `folder` (defaults = the brief's own sample records)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "crm_customers.json").write_text(json.dumps(GOOD_CRM if crm is None else crm), encoding="utf-8")
    (folder / "orders.csv").write_text(GOOD_ORDERS if orders is None else orders, encoding="utf-8")
    (folder / "support_tickets.xml").write_text(GOOD_TICKETS if tickets is None else tickets, encoding="utf-8")
    return folder


@pytest.fixture
def config(tmp_path):
    """The real config.json, but pointing input/output/logs at a temporary folder."""
    cfg = load_config()
    cfg["input_dir"] = tmp_path / "raw"
    cfg["output_dir"] = tmp_path / "output"
    cfg["log_dir"] = tmp_path / "logs"
    setup_logging(cfg["log_dir"])
    return cfg


def source_cfg(config, name):
    """The config block of one source, e.g. source_cfg(config, 'orders')."""
    return next(s for s in config["input_sources"] if s["name"] == name)
