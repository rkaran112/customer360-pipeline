"""
ALERTS - tell people when the incoming data looks wrong, without them having to look.

After every run, evaluate_alerts() turns the numbers into graded alerts:

    INFO      worth knowing           e.g. "3 invalid emails in crm_customers.json"
    WARNING   someone should look     e.g. "crm: 0.8% of records quarantined (limit 0.5%)"
    CRITICAL  do NOT trust this run   e.g. "orders: 12% quarantined", "a source file is empty"

What is checked (all limits live in config.json -> "alerts"):
    QUARANTINE_RATE   % of a source's records quarantined          WARNING / CRITICAL above limits
    RULE_FAILURES     each rule that rejected rows, with the count INFO
    EMPTY_SOURCE      a source sent 0 records                      CRITICAL
    SCHEMA_DRIFT      unexpected / missing optional columns         WARNING
    VOLUME_CHANGE     record count moved a lot vs the last run      WARNING (catches half-sent files)
    DQ_SCORE_DROP     quality score fell vs the last run            WARNING
    ORPHAN_RECORDS    orders/tickets with no customer to attach to  INFO
    CHECK_FAILED      a step-6 reconciliation check failed          CRITICAL

Where alerts go (notify()):
    - always: the console + log files, and output/alerts.json (the dashboard shows them)
    - optionally: a WEBHOOK (Slack / Microsoft Teams / Discord / any HTTP endpoint). Put the
      webhook URL in the environment variable named in config ("C360_ALERT_WEBHOOK").
      It is never written in code or config, because a webhook URL is a secret.

A CRITICAL alert makes the run fail (exit code 1), so bad data is never published as "OK".
"""
import json
import os
import urllib.request

from pipeline.diagnostics import HINTS
from pipeline.logger import get_logger

log = get_logger()

SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}

# Plain-English advice per alert type, shown next to the alert.
# >>> TO CHANGE: edit the advice people see for each alert type.
ADVICE = {
    "QUARANTINE_RATE": "Open quarantine.csv, group by reason, and send the rows back to the source team.",
    "RULE_FAILURES": "See quarantine.csv for the exact records and values.",
    "EMPTY_SOURCE": "The source sent no data. Check the export job on the source system before publishing.",
    "SCHEMA_DRIFT": HINTS["SchemaDriftError"],
    "VOLUME_CHANGE": "A big jump or drop often means a partial or duplicated export. Compare with the source.",
    "DQ_SCORE_DROP": "Data got dirtier than last run. Check which rule grew in quarantine.csv.",
    "ORPHAN_RECORDS": "These belong to customers whose CRM record was quarantined; they rejoin once the CRM record is fixed.",
    "CHECK_FAILED": "Do not use this run's numbers. Read the check details and logs/pipeline.log.",
}


def _alert(severity, code, message, source=None, value=None, threshold=None):
    return {"severity": severity, "code": code, "source": source, "message": message,
            "value": value, "threshold": threshold, "advice": ADVICE.get(code, "")}


def at_least(alerts, severity):
    """Alerts with this severity or worse."""
    return [a for a in alerts if SEVERITY_ORDER[a["severity"]] >= SEVERITY_ORDER[severity]]


# ----------------------------------------------------------------- deciding the alerts

def evaluate_alerts(dq_report, check_results, config, previous_run=None):
    """Turn one run's numbers into a list of alerts, worst first."""
    limits = config.get("alerts", {})
    warn_pct = limits.get("quarantine_rate_warning_pct", 0.5)
    crit_pct = limits.get("quarantine_rate_critical_pct", 5.0)
    files = {s["name"]: s["file"] for s in config["input_sources"]}
    alerts = []

    for name, s in dq_report["by_source"].items():
        # EMPTY_SOURCE: nothing arrived at all.
        if s["total"] == 0:
            alerts.append(_alert("CRITICAL", "EMPTY_SOURCE", f"{files.get(name, name)} contains 0 records", name, 0))
            continue

        # QUARANTINE_RATE: share of this source's records that were rejected.
        rate = round(100 * s["quarantined"] / s["total"], 2)
        if rate >= crit_pct:
            alerts.append(_alert("CRITICAL", "QUARANTINE_RATE",
                                 f"{name}: {rate}% of records quarantined ({s['quarantined']} of {s['total']}), "
                                 f"critical limit {crit_pct}%", name, rate, crit_pct))
        elif rate >= warn_pct:
            alerts.append(_alert("WARNING", "QUARANTINE_RATE",
                                 f"{name}: {rate}% of records quarantined ({s['quarantined']} of {s['total']}), "
                                 f"warning limit {warn_pct}%", name, rate, warn_pct))

        # RULE_FAILURES: one INFO line per rule that rejected something.
        for rule_name, count in s["failures"].items():
            alerts.append(_alert("INFO", "RULE_FAILURES",
                                 f"{count} record(s) failed '{rule_name}' in {files.get(name, name)}", name, count))

    # SCHEMA_DRIFT: column changes found in step 2.
    for warning in dq_report.get("schema_drift_warnings", []):
        alerts.append(_alert("WARNING", "SCHEMA_DRIFT", warning))

    # ORPHAN_RECORDS: clean records with no customer to attach to.
    integrity = dq_report.get("referential_integrity", {})
    orphans = integrity.get("orders_without_customer", 0) + integrity.get("tickets_without_customer", 0)
    if orphans:
        alerts.append(_alert("INFO", "ORPHAN_RECORDS", f"{orphans} order/ticket record(s) have no clean customer "
                                                      f"to attach to", value=orphans))

    # Compared with the previous run (only if there is one).
    if previous_run:
        alerts.extend(_compare_with_previous(dq_report, previous_run, limits))

    # CHECK_FAILED: any step-6 check failing is always critical.
    for name, ok, details in check_results:
        if not ok:
            alerts.append(_alert("CRITICAL", "CHECK_FAILED", f"check '{name}' failed: {details}"))

    return sorted(alerts, key=lambda a: -SEVERITY_ORDER[a["severity"]])


def _compare_with_previous(dq_report, previous, limits):
    """VOLUME_CHANGE and DQ_SCORE_DROP: is this run very different from the last one?"""
    alerts = []
    volume_limit = limits.get("volume_change_warning_pct", 50)
    for name, s in dq_report["by_source"].items():
        before = previous.get("records_by_source", {}).get(name)
        if before:                              # skip if unknown or 0 (no % change possible)
            change = round(100 * (s["total"] - before) / before, 1)
            if abs(change) >= volume_limit:
                alerts.append(_alert("WARNING", "VOLUME_CHANGE",
                                     f"{name}: {s['total']} records vs {before} last run ({change:+}%)",
                                     name, change, volume_limit))
    drop_limit = limits.get("dq_score_drop_warning_points", 1.0)
    before_score = previous.get("dq_score_pct")
    if before_score is not None:
        drop = round(before_score - dq_report["dq_score_pct"], 2)
        if drop >= drop_limit:
            alerts.append(_alert("WARNING", "DQ_SCORE_DROP",
                                 f"DQ score fell {drop} points: {before_score}% -> {dq_report['dq_score_pct']}%",
                                 value=drop, threshold=drop_limit))
    return alerts


# ----------------------------------------------------------------- sending the alerts

def build_message(alerts, run_summary, project="Customer 360"):
    """A short human message, used for the webhook (Slack/Teams/Discord all show 'text')."""
    icon = {"CRITICAL": "🔴", "WARNING": "🟠", "INFO": "🔵"}
    worst = alerts[0]["severity"] if alerts else "INFO"
    lines = [f"{icon[worst]} {project} pipeline run {run_summary.get('run_id', '?')}: "
             f"{'FAILED' if not run_summary.get('ok', True) else 'finished'} with "
             f"{len(at_least(alerts, 'CRITICAL'))} critical, {len(at_least(alerts, 'WARNING')) - len(at_least(alerts, 'CRITICAL'))} warning alert(s)"]
    if "dq_score_pct" in run_summary:
        lines.append(f"DQ score {run_summary['dq_score_pct']}% · {run_summary.get('quarantined_records', 0)} "
                     f"quarantined of {run_summary.get('total_records', 0)} records")
    for a in alerts:
        if a["severity"] != "INFO":
            lines.append(f"{icon[a['severity']]} {a['message']}  → {a['advice']}")
    return "\n".join(lines)


def send_webhook(url, alerts, run_summary, timeout=5):
    """POST the alerts as JSON. Never raises: a broken webhook must not break the pipeline."""
    text = build_message(alerts, run_summary)
    # "text" is read by Slack and Teams, "content" by Discord; the rest is for custom receivers.
    payload = {"text": text, "content": text[:1900], "run": run_summary, "alerts": alerts}
    request = urllib.request.Request(url, data=json.dumps(payload, default=str).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"sent": True, "status": response.status}
    except Exception as error:      # network down, wrong URL, 4xx/5xx ... log it and carry on
        log.warning("  alert webhook failed (%s): %s", type(error).__name__, error)
        return {"sent": False, "error": f"{type(error).__name__}: {error}"}


def notify(alerts, run_summary, config):
    """Log every alert, and send the important ones to the webhook (if configured)."""
    limits = config.get("alerts", {})
    for a in alerts:
        level = {"INFO": log.info, "WARNING": log.warning, "CRITICAL": log.error}[a["severity"]]
        level("  ALERT %-8s %-16s %s", a["severity"], a["code"], a["message"],
              extra={"alert_code": a["code"], "severity": a["severity"]})

    to_send = at_least(alerts, limits.get("notify_min_severity", "WARNING"))
    url = os.environ.get(limits.get("webhook_env_var", "C360_ALERT_WEBHOOK"), "").strip()
    if not to_send:
        return {"sent": False, "reason": "no alerts at or above notify_min_severity"}
    if not url:
        return {"sent": False, "reason": "no webhook configured"}
    result = send_webhook(url, to_send, run_summary)
    if result["sent"]:
        log.info("  sent %d alert(s) to the webhook", len(to_send))
    return result
