"""DASHBOARD smoke tests - the app starts, every tab renders, lookup works (Streamlit's own AppTest)."""
from streamlit.testing.v1 import AppTest

from pipeline.config import PROJECT_ROOT

APP = str(PROJECT_ROOT / "streamlit_app.py")


def test_app_runs_without_errors_and_has_all_tabs():
    """The dashboard starts with no exception and shows 9 tabs. Why: a broken demo page is the worst possible failure."""
    at = AppTest.from_file(APP, default_timeout=180).run()
    assert not at.exception
    assert len(at.tabs) == 9
    assert len(at.metric) >= 10


def test_customer_lookup_shows_the_customer():
    """Searching C0001 shows that customer's card with a masked email. Why: the '360 view' of one customer is the core feature."""
    at = AppTest.from_file(APP, default_timeout=180).run()
    at.text_input(key="lookup").set_value("C0001").run()
    assert not at.exception
    assert any("C0001" in m.value for m in at.markdown)
    assert any("us***@example.com" in c.value for c in at.caption)


def test_chaos_lab_runs_a_scenario_from_the_dashboard():
    """Clicking 'Run scenario' in the Chaos Lab runs it and shows a passing result. Why: the live demo of recovery must work."""
    at = AppTest.from_file(APP, default_timeout=180).run()
    at.button[[b.label for b in at.button].index("💥 Run scenario")].click().run(timeout=180)
    assert not at.exception
    assert any(e.label.startswith("✅") and "Corrupted JSON" in e.label for e in at.expander)
