"""ALERT tests - the right alert at the right severity, and the webhook behaves (and never breaks a run)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import run_pipeline
from pipeline import alerts
from tests.conftest import GOOD_CRM, write_sources


def report(by_source, drift=None, integrity=None, score=99.0):
    """A minimal dq_report with the given per-source numbers."""
    return {"by_source": by_source, "schema_drift_warnings": drift or [], "referential_integrity": integrity or {},
            "dq_score_pct": score}


def src(total, quarantined, failures=None):
    return {"total": total, "quarantined": quarantined, "failures": failures or {}}


def codes(alert_list, severity=None):
    return [a["code"] for a in alert_list if severity is None or a["severity"] == severity]


def test_quarantine_rate_warning(config):
    """1% quarantined (limit 0.5%) -> WARNING. Why: someone should look before it gets worse."""
    found = alerts.evaluate_alerts(report({"crm": src(100, 1)}), [], config)
    assert "QUARANTINE_RATE" in codes(found, "WARNING")


def test_quarantine_rate_critical(config):
    """10% quarantined (limit 5%) -> CRITICAL. Why: an upstream system is clearly broken."""
    found = alerts.evaluate_alerts(report({"orders": src(100, 10)}), [], config)
    assert "QUARANTINE_RATE" in codes(found, "CRITICAL")


def test_low_quarantine_rate_gives_no_rate_alert(config):
    """0.2% quarantined -> no rate alert. Why: a few bad rows are normal; alert fatigue makes people ignore alerts."""
    found = alerts.evaluate_alerts(report({"tickets": src(500, 1)}), [], config)
    assert "QUARANTINE_RATE" not in codes(found)


def test_each_failing_rule_gives_an_info_alert_with_count(config):
    """3 invalid emails -> an INFO alert saying '3 record(s) failed invalid_emails'. Why: the user asked to be told the counts."""
    found = alerts.evaluate_alerts(report({"crm": src(500, 4, {"invalid_emails": 3, "duplicates": 1})}), [], config)
    info = [a for a in found if a["code"] == "RULE_FAILURES"]
    assert len(info) == 2 and any("3 record(s) failed 'invalid_emails'" in a["message"] for a in info)


def test_empty_source_is_critical(config):
    """A source with 0 records -> CRITICAL. Why: an empty export is almost always a failure upstream."""
    assert "EMPTY_SOURCE" in codes(alerts.evaluate_alerts(report({"tickets": src(0, 0)}), [], config), "CRITICAL")


def test_schema_drift_is_a_warning(config):
    """Unexpected columns -> WARNING. Why: harmless today, but the data contract has changed."""
    found = alerts.evaluate_alerts(report({}, drift=["orders: unexpected column(s) ['x']"]), [], config)
    assert codes(found) == ["SCHEMA_DRIFT"]


def test_volume_change_vs_previous_run(config):
    """500 -> 200 records (-60%) -> WARNING. Why: catches half-sent files that are still valid."""
    previous = {"records_by_source": {"orders": 500}, "dq_score_pct": 99.0}
    found = alerts.evaluate_alerts(report({"orders": src(200, 0)}), [], config, previous)
    assert "VOLUME_CHANGE" in codes(found, "WARNING")


def test_no_previous_run_means_no_comparison_alerts(config):
    """First run ever -> no volume/DQ-drop alerts. Why: there is nothing to compare with."""
    found = alerts.evaluate_alerts(report({"orders": src(200, 0)}), [], config, None)
    assert not {"VOLUME_CHANGE", "DQ_SCORE_DROP"} & set(codes(found))


def test_dq_score_drop_vs_previous_run(config):
    """DQ score 99.5% -> 97% -> WARNING. Why: data getting dirtier is an early warning sign."""
    previous = {"records_by_source": {}, "dq_score_pct": 99.5}
    found = alerts.evaluate_alerts(report({"crm": src(100, 0)}, score=97.0), [], config, previous)
    assert "DQ_SCORE_DROP" in codes(found)


def test_failed_check_is_critical(config):
    """A failed step-6 check -> CRITICAL. Why: the numbers cannot be trusted."""
    found = alerts.evaluate_alerts(report({}), [("revenue conserved", False, "x")], config)
    assert codes(found, "CRITICAL") == ["CHECK_FAILED"]


def test_alerts_sorted_worst_first(config):
    """CRITICAL alerts come before WARNING and INFO. Why: the most important line is read first."""
    found = alerts.evaluate_alerts(report({"a": src(100, 1, {"duplicates": 1}), "b": src(0, 0)}), [], config)
    order = [alerts.SEVERITY_ORDER[a["severity"]] for a in found]
    assert order == sorted(order, reverse=True)


# ----------------------------------------------------------------- webhook

class _Receiver(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Receiver.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def webhook(monkeypatch):
    """A tiny local HTTP server standing in for Slack/Teams; its URL goes in the env variable."""
    _Receiver.received = []
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("C360_ALERT_WEBHOOK", f"http://127.0.0.1:{server.server_port}/hook")
    yield _Receiver.received
    server.shutdown()


def test_webhook_receives_warning_and_critical_only(config, webhook):
    """The webhook gets WARNING+ alerts, not INFO, in Slack/Teams/Discord format. Why: chat channels must stay readable."""
    found = alerts.evaluate_alerts(report({"crm": src(100, 1, {"duplicates": 1})}), [], config)
    result = alerts.notify(found, {"run_id": "t1", "ok": True, "dq_score_pct": 99.0}, config)
    assert result["sent"] is True and len(webhook) == 1
    payload = webhook[0]
    assert "text" in payload and "content" in payload
    assert {a["severity"] for a in payload["alerts"]} == {"WARNING"}


def test_no_webhook_configured_means_nothing_sent(config, monkeypatch):
    """No env variable -> no network call, run continues. Why: alerts must work offline and on a laptop."""
    monkeypatch.delenv("C360_ALERT_WEBHOOK", raising=False)
    found = alerts.evaluate_alerts(report({"crm": src(100, 10)}), [], config)
    assert alerts.notify(found, {"run_id": "t2"}, config) == {"sent": False, "reason": "no webhook configured"}


def test_unreachable_webhook_never_raises(config, monkeypatch):
    """A dead webhook URL is logged, not raised. Why: a Slack outage must never stop the data pipeline."""
    monkeypatch.setenv("C360_ALERT_WEBHOOK", "http://127.0.0.1:9/nothing-here")
    found = alerts.evaluate_alerts(report({"crm": src(100, 10)}), [], config)
    result = alerts.notify(found, {"run_id": "t3"}, config)
    assert result["sent"] is False and "error" in result


def test_critical_alert_fails_the_run_and_blocks_publishing(config, tmp_path):
    """Half the orders negative -> CRITICAL -> exit 1 and outputs NOT published. Why: bad data must never look like success."""
    orders = "cust_id,total_orders,lifetime_value,last_order_date\nC0001,1,-100,2026-08-10\nC0002,7,35000,2026-08-02\n"
    write_sources(config["input_dir"], crm=GOOD_CRM, orders=orders)
    outcome = run_pipeline.execute(input_dir=config["input_dir"], output_dir=config["output_dir"],
                                   log_dir=config["log_dir"])
    assert outcome["exit_code"] == 1 and "QUARANTINE_RATE" in codes(outcome["alerts"], "CRITICAL")
    assert not (config["output_dir"] / "customer_360.csv").exists()
    assert (config["output_dir"] / "failed_runs" / outcome["run_id"] / "customer_360.csv").exists()
