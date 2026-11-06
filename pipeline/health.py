"""
HEALTH - one answer to "is the pipeline OK right now?", computed from output/run_history.jsonl.

    HEALTHY    last run succeeded, no warnings, data is fresh
    DEGRADED   last run succeeded but raised WARNING alerts, or a recent run failed, or data is stale
    FAILING    the last run failed (data problem or crash)
    UNKNOWN    no runs yet

Also returns the numbers an on-call engineer looks at first (the "golden signals" of a batch job):
    success rate over the last N runs, last success / last failure (with its error),
    freshness (how long since the last good run), and run duration (average and slowest).

The dashboard re-reads this every few seconds, so it shows LIVE health while runs happen.
"""
from datetime import datetime


def compute_health(history, config, now=None):
    """Turn the run history into a health summary dict."""
    now = now or datetime.now()
    settings = config.get("health", {})
    window = settings.get("window_runs", 20)
    stale_hours = settings.get("stale_after_hours", 26)   # a daily job: 24h + 2h grace

    if not history:
        return {"status": "UNKNOWN", "reasons": ["the pipeline has not run yet"], "runs": 0}

    recent = history[-window:]
    last = recent[-1]
    successes = [r for r in history if r.get("ok")]
    failures = [r for r in history if not r.get("ok")]
    last_success, last_failure = (successes[-1] if successes else None), (failures[-1] if failures else None)
    durations = [r["duration_seconds"] for r in recent if r.get("duration_seconds") is not None]

    hours_since_success = None
    if last_success:
        hours_since_success = round((now - datetime.fromisoformat(last_success["finished_at"])).total_seconds() / 3600, 2)

    # WHAT: decide the status. Reasons are kept so the panel can say WHY it isn't healthy.
    reasons = []
    if not last.get("ok"):
        status = "FAILING"
        reasons.append(f"last run {last['run_id']} failed: {last.get('error') or last.get('status')}")
    else:
        status = "HEALTHY"
        if last.get("alerts", {}).get("WARNING", 0):
            status = "DEGRADED"
            reasons.append(f"last run raised {last['alerts']['WARNING']} warning alert(s)")
        recent_failures = sum(1 for r in recent if not r.get("ok"))
        if recent_failures:
            status = "DEGRADED"
            reasons.append(f"{recent_failures} of the last {len(recent)} runs failed (recovered since)")
    if hours_since_success is not None and hours_since_success > stale_hours:
        status = "FAILING" if status == "FAILING" else "DEGRADED"
        reasons.append(f"data is stale: last good run {hours_since_success} h ago (limit {stale_hours} h)")
    if not reasons:
        reasons.append("last run succeeded with no warnings")

    return {
        "status": status,
        "reasons": reasons,
        "runs": len(history),
        "window": len(recent),
        "success_rate_pct": round(100 * sum(1 for r in recent if r.get("ok")) / len(recent), 1),
        "last_run": last,
        "last_success_at": last_success["finished_at"] if last_success else None,
        "last_failure": last_failure,
        "hours_since_success": hours_since_success,
        "avg_duration_s": round(sum(durations) / len(durations), 3) if durations else None,
        "max_duration_s": max(durations) if durations else None,
    }
