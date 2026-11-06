"""HEALTH + CHAOS tests - the health status is right, and every injected fault is handled and recovered from."""
from datetime import datetime, timedelta

import pytest

from pipeline import chaos
from pipeline.health import compute_health

NOW = datetime(2026, 11, 6, 12, 0, 0)


def run(ok=True, hours_ago=1, warnings=0, run_id="r"):
    return {"run_id": run_id, "ok": ok, "finished_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "duration_seconds": 0.5, "alerts": {"WARNING": warnings}, "status": "SUCCESS" if ok else "FAILED_DATA",
            "error": None if ok else "FileNotFoundError: x"}


def test_no_runs_is_unknown(config):
    """No history -> UNKNOWN. Why: never claim 'healthy' without evidence."""
    assert compute_health([], config, NOW)["status"] == "UNKNOWN"


def test_clean_runs_are_healthy(config):
    """Recent successful runs without warnings -> HEALTHY, 100% success."""
    h = compute_health([run(), run()], config, NOW)
    assert h["status"] == "HEALTHY" and h["success_rate_pct"] == 100.0


def test_warning_alerts_make_it_degraded(config):
    """Last run OK but with WARNING alerts -> DEGRADED. Why: working, but someone should look."""
    assert compute_health([run(warnings=1)], config, NOW)["status"] == "DEGRADED"


def test_last_run_failed_is_failing(config):
    """Last run failed -> FAILING, with the error as the reason. Why: the first thing on-call must see."""
    h = compute_health([run(), run(ok=False, run_id="bad")], config, NOW)
    assert h["status"] == "FAILING" and "bad" in h["reasons"][0] and h["last_failure"]["run_id"] == "bad"


def test_recovered_after_failure_is_degraded_not_healthy(config):
    """Fail then succeed -> DEGRADED ('recovered since'), success rate 50%. Why: recent instability is still worth knowing."""
    h = compute_health([run(ok=False), run()], config, NOW)
    assert h["status"] == "DEGRADED" and h["success_rate_pct"] == 50.0
    assert any("recovered" in r for r in h["reasons"])


def test_stale_data_is_flagged(config):
    """Last good run 30 h ago (limit 26 h) -> DEGRADED, stale. Why: a pipeline that silently stopped running looks 'green' otherwise."""
    h = compute_health([run(hours_ago=30)], config, NOW)
    assert h["status"] == "DEGRADED" and any("stale" in r for r in h["reasons"])


@pytest.mark.parametrize("name", [s.name for s in chaos.SCENARIOS])
def test_chaos_scenario_is_handled_and_recovers(name):
    """Each injected fault gives the expected exit code and signal, keeps last good outputs, and recovers on the next run.
    Why: proves failure handling works for real, not just on paper."""
    result = chaos.run_scenario(name)
    failed = [c for c in result["checks"] if not c["passed"]]
    assert result["passed"], failed
