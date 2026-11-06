"""Configuration tests - the pipeline is driven by config.json, not hard-coded values."""
import json

import pytest

from pipeline import insights
from pipeline.config import REQUIRED_SECTIONS, load_config
from tests.test_insights import make_360


def test_real_config_loads_with_all_sections():
    """config.json loads and has every section the code needs. Why: catches a broken config before any data is touched."""
    config = load_config()
    for section in REQUIRED_SECTIONS:
        assert section in config
    assert [s["file"] for s in config["input_sources"]] == ["crm_customers.json", "orders.csv", "support_tickets.xml"]


def test_config_missing_section_fails_clearly(tmp_path):
    """A config without 'segments' fails with a clear message. Why: fail fast instead of a KeyError deep inside a step."""
    bad = load_config()
    del bad["segments"]
    for key in ["input_dir", "output_dir", "log_dir"]:
        bad[key] = str(bad[key])
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="segments"):
        load_config(path)


def test_changing_gold_threshold_in_config_changes_segments(config):
    """Gold threshold 150000 in config -> a 120000 customer becomes Silver, with no code change. Why: proves 'config driven'."""
    df = make_360([120000])
    assert insights.enrich(df, config)[0].loc[0, "segment"] == "Gold"
    config["segments"]["gold_above"] = 150000
    assert insights.enrich(df, config)[0].loc[0, "segment"] == "Silver"
