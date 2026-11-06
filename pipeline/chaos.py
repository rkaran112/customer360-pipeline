"""
CHAOS TESTING - break things on purpose and PROVE the pipeline handles it.

"Chaos engineering" (made famous by Netflix) means injecting real failures to check that a
system fails safely and recovers, instead of hoping it does. Each scenario below:

    1. copies the real data into a temporary sandbox and runs a GOOD baseline run
    2. injects ONE fault (a broken file, a missing column, a crash inside a step ...)
    3. runs the pipeline again and checks it reacted the RIGHT way:
         - the expected exit code (0 ok / 1 data problem / 2 bug)
         - the expected signal (an alert or a clear error naming the problem)
         - if the run failed: the last good outputs are untouched, the failure is in
           the run history, and a crash report was written when there was an error
    4. removes the fault and runs again: it must succeed (= it RECOVERS by itself, no manual cleanup)

Nothing here touches the real data/ or output/ folders.

Run from the command line:   python chaos_test.py            (all scenarios)
                             python chaos_test.py --list
                             python chaos_test.py --scenario corrupt_json
Or in the dashboard:         the "Chaos Lab" tab.
"""
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pipeline import extract, merge
from pipeline.config import load_config
from pipeline.health import compute_health
from pipeline.outputs import read_run_history


# ----------------------------------------------------------------- fault injectors
# Each injector gets the sandbox input folder and the config, changes something, and may
# return a "patch" context manager (for faults that are not in the files, e.g. a code crash).

def _file(folder, config, name):
    return Path(folder) / next(s["file"] for s in config["input_sources"] if s["name"] == name)


def _rewrite_csv(path, transform):
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(transform(lines)) + "\n", encoding="utf-8")


def inject_nothing(folder, config):
    return None


def inject_corrupt_json(folder, config):
    """Cut the CRM JSON file in half, as if the export was interrupted."""
    path = _file(folder, config, "crm")
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])


def inject_missing_file(folder, config):
    """The orders export never arrived."""
    _file(folder, config, "orders").unlink()


def inject_drop_mandatory_column(folder, config):
    """The order system stopped sending lifetime_value (a mandatory column)."""
    def drop(lines):
        header = lines[0].split(",")
        i = header.index("lifetime_value")
        return [",".join(c for j, c in enumerate(line.split(",")) if j != i) for line in lines]
    _rewrite_csv(_file(folder, config, "orders"), drop)


def inject_extra_column(folder, config):
    """The order system added a new column nobody told us about."""
    _rewrite_csv(_file(folder, config, "orders"),
                 lambda lines: [lines[0] + ",coupon_code"] + [line + ",NONE" for line in lines[1:]])


def inject_empty_source(folder, config):
    """The support system sent a valid but EMPTY file."""
    _file(folder, config, "tickets").write_text("<?xml version='1.0' encoding='utf-8'?>\n<customers></customers>\n",
                                                encoding="utf-8")


def inject_bad_row_spike(folder, config):
    """A bug upstream makes 20% of orders have negative revenue."""
    def spike(lines):
        out = [lines[0]]
        for n, line in enumerate(lines[1:]):
            parts = line.split(",")
            if n % 5 == 0 and parts[2]:
                parts[2] = "-" + parts[2].lstrip("-")
            out.append(",".join(parts))
        return out
    _rewrite_csv(_file(folder, config, "orders"), spike)


def inject_duplicate_flood(folder, config):
    """CRM sends 10 customers twice (about 2% duplicates)."""
    path = _file(folder, config, "crm")
    records = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(records + records[:10]), encoding="utf-8")


def inject_partial_export(folder, config):
    """Only 40% of the orders file arrived (export stopped early, but the file is still valid)."""
    _rewrite_csv(_file(folder, config, "orders"), lambda lines: lines[: 1 + int((len(lines) - 1) * 0.4)])


def inject_bad_encoding(folder, config):
    """The orders file was saved as UTF-16 instead of UTF-8."""
    path = _file(folder, config, "orders")
    path.write_bytes(path.read_text(encoding="utf-8").encode("utf-16"))


@contextlib.contextmanager
def _flaky_csv_reader(failures=2):
    """The CSV file is locked for a moment (e.g. still being copied): the first 2 reads fail."""
    original = extract.READERS["csv"]
    state = {"calls": 0}

    def flaky(path, source):
        state["calls"] += 1
        if state["calls"] <= failures:
            raise PermissionError(f"[chaos] {path.name} is locked by another process")
        return original(path, source)
    extract.READERS["csv"] = flaky
    try:
        yield
    finally:
        extract.READERS["csv"] = original


def inject_flaky_disk(folder, config):
    return _flaky_csv_reader()


@contextlib.contextmanager
def _webhook_down(config):
    """The alert webhook (Slack/Teams) is unreachable."""
    var = config.get("alerts", {}).get("webhook_env_var", "C360_ALERT_WEBHOOK")
    old = os.environ.get(var)
    os.environ[var] = "http://127.0.0.1:9/unreachable-webhook"      # port 9: nothing listens
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = old


def inject_webhook_down(folder, config):
    return _webhook_down(config)


@contextlib.contextmanager
def _crash_in_merge():
    """A bug: the merge step raises an unexpected Python error."""
    original = merge.build_customer_360

    def broken(clean_tables):
        raise RuntimeError("[chaos] simulated bug inside the merge step")
    merge.build_customer_360 = broken
    try:
        yield
    finally:
        merge.build_customer_360 = original


def inject_code_crash(folder, config):
    return _crash_in_merge()


# ----------------------------------------------------------------- the scenario catalogue

@dataclass
class Scenario:
    name: str
    title: str
    fault: str                 # what we break
    expected_exit: int         # 0 ok, 1 data problem, 2 bug
    expected_signal: str       # alert code or error type that must appear
    recovery: str              # how the pipeline protects itself
    inject: callable


# >>> TO CHANGE: a new chaos scenario = one injector function above + one Scenario line here.
SCENARIOS = [
    Scenario("baseline", "Clean data (control run)", "nothing - proves the sandbox works", 0, "QUARANTINE_RATE",
             "normal run; the known 7 bad rows are quarantined and raise a WARNING", inject_nothing),
    Scenario("corrupt_json", "Corrupted JSON file", "CRM file cut in half mid-export", 1, "SourceFileError",
             "stops at Extract with the file name + line; last good outputs kept", inject_corrupt_json),
    Scenario("missing_file", "Missing source file", "orders.csv never arrived", 1, "FileNotFoundError",
             "stops at Extract and names the missing file; last good outputs kept", inject_missing_file),
    Scenario("drop_mandatory_column", "Schema drift: column removed", "orders.csv lost lifetime_value", 1,
             "SchemaDriftError", "schema drift guard stops the run before any wrong revenue is published",
             inject_drop_mandatory_column),
    Scenario("extra_column", "Schema drift: new column", "orders.csv gained an unknown column", 0, "SCHEMA_DRIFT",
             "unknown column ignored, WARNING alert raised, run continues", inject_extra_column),
    Scenario("empty_source", "Empty source file", "support system sent 0 records", 1, "EMPTY_SOURCE",
             "CRITICAL alert, outputs NOT published; last good outputs kept", inject_empty_source),
    Scenario("bad_row_spike", "Bad-row spike", "20% of orders have negative revenue", 1, "QUARANTINE_RATE",
             "bad rows quarantined, quarantine rate > 5% = CRITICAL, outputs NOT published", inject_bad_row_spike),
    Scenario("duplicate_flood", "Duplicate flood", "10 CRM customers sent twice", 0, "QUARANTINE_RATE",
             "duplicates quarantined, WARNING raised, clean data still published", inject_duplicate_flood),
    Scenario("partial_export", "Partial export", "only 40% of orders.csv arrived", 0, "VOLUME_CHANGE",
             "volume compared with the last good run: WARNING (-60%)", inject_partial_export),
    Scenario("bad_encoding", "Wrong file encoding", "orders.csv saved as UTF-16", 1, "SourceFileError",
             "clear 'not UTF-8' error with a fix hint; last good outputs kept", inject_bad_encoding),
    Scenario("flaky_disk", "Flaky file system", "orders.csv locked for the first 2 reads", 0, "retry",
             "retry with backoff (0.2s, 0.4s) - succeeds on the 3rd attempt, no human needed", inject_flaky_disk),
    Scenario("webhook_down", "Alert channel down", "Slack/Teams webhook unreachable", 0, "webhook_failed",
             "webhook failure is logged, never crashes the pipeline", inject_webhook_down),
    Scenario("code_crash", "Bug inside a step", "merge step raises an unexpected error", 2, "RuntimeError",
             "exit code 2 (= bug), crash report with stage + traceback; last good outputs kept", inject_code_crash),
]
BY_NAME = {s.name: s for s in SCENARIOS}


# ----------------------------------------------------------------- running a scenario

def _md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


def _signal_seen(scenario, outcome, log_text):
    """Did the expected alert / error / behaviour actually happen?"""
    codes = {a["code"] for a in outcome.get("alerts", [])}
    error = outcome.get("error")
    if scenario.expected_signal == "retry":
        retries = (outcome.get("summary") or {}).get("retries", 0)
        return retries == 2, f"{retries} retries recorded in the run summary"
    if scenario.expected_signal == "webhook_failed":
        sent = (outcome.get("summary") or {}).get("notification", {})
        return sent.get("sent") is False and "error" in sent, f"notification result: {sent}"
    if error is not None:
        return type(error).__name__ == scenario.expected_signal, f"{type(error).__name__}: {error}"
    return scenario.expected_signal in codes, f"alerts raised: {sorted(codes)}"


def run_scenario(name, project_input=None, keep_sandbox=False):
    """Run one chaos scenario in a sandbox. Returns a result dict with every verification."""
    import run_pipeline                                # imported here to avoid a circular import
    scenario = BY_NAME[name]
    config = load_config()
    source_dir = Path(project_input or config["input_dir"])
    sandbox = Path(tempfile.mkdtemp(prefix=f"chaos_{name}_"))
    raw, out, logs = sandbox / "raw", sandbox / "output", sandbox / "logs"
    shutil.copytree(source_dir, raw)
    started = time.perf_counter()
    checks = []
    run = lambda: run_pipeline.execute(input_dir=raw, output_dir=out, log_dir=logs, log_level="INFO")

    # 1. baseline good run -> the "last known good" outputs
    baseline = run()
    good_md5 = _md5(out / "customer_360.csv")
    checks.append(("baseline run succeeded", baseline["exit_code"] == 0, f"exit {baseline['exit_code']}"))

    # 2. inject the fault and run
    sandbox_config = dict(config, input_dir=raw)
    patch = scenario.inject(raw, sandbox_config) or contextlib.nullcontext()
    log_file = logs / "pipeline.log"
    log_size_before = log_file.stat().st_size if log_file.exists() else 0
    with patch:
        outcome = run()
    log_text = log_file.read_text(encoding="utf-8", errors="replace")[log_size_before:] if log_file.exists() else ""

    # 3. verify the reaction
    checks.append(("exit code as expected", outcome["exit_code"] == scenario.expected_exit,
                   f"got {outcome['exit_code']}, expected {scenario.expected_exit}"))
    seen, detail = _signal_seen(scenario, outcome, log_text)
    checks.append((f"signal '{scenario.expected_signal}' raised", seen, detail))
    if scenario.expected_exit != 0:
        checks.append(("last good outputs untouched", _md5(out / "customer_360.csv") == good_md5,
                       "customer_360.csv identical to the baseline"))
        last = read_run_history(out)[-1]
        checks.append(("failure recorded in run history", last["run_id"] == outcome["run_id"] and not last["ok"],
                       f"status {last.get('status')}"))
        if "error" in outcome:
            report = Path(outcome["crash_report"])
            ok = report.exists() and json.loads(report.read_text(encoding="utf-8")).get("stage") == outcome["stage"]
            checks.append(("crash report written", ok, f"stage '{outcome['stage']}' -> {report.name}"))
    else:
        checks.append(("outputs published", outcome["ok"] and (out / "customer_360.csv").exists(),
                       f"run {outcome['run_id']}"))

    # 4. remove the fault (fresh copy of the good files) and run again: must recover by itself
    shutil.rmtree(raw)
    shutil.copytree(source_dir, raw)
    recovered = run()
    checks.append(("recovers on the next clean run", recovered["exit_code"] == 0, f"exit {recovered['exit_code']}"))

    history = read_run_history(out)
    result = {
        "scenario": scenario.name, "title": scenario.title, "fault": scenario.fault,
        "expected_exit": scenario.expected_exit, "actual_exit": outcome["exit_code"],
        "recovery": scenario.recovery, "passed": all(ok for _, ok, _ in checks),
        "checks": [{"check": c, "passed": ok, "detail": d} for c, ok, d in checks],
        "stage": outcome.get("stage"), "error": (outcome.get("summary") or {}).get("error"),
        "hint": (outcome.get("summary") or {}).get("hint"),
        "alerts": [{k: a[k] for k in ("severity", "code", "message")} for a in outcome.get("alerts", [])
                   if a["severity"] != "INFO"],
        "health_after": compute_health(history, config)["status"],
        "history": history,
        "seconds": round(time.perf_counter() - started, 2),
        "sandbox": str(sandbox),
    }
    if not keep_sandbox:
        shutil.rmtree(sandbox, ignore_errors=True)
    return result


def run_all(names=None, project_input=None):
    """Run several scenarios (default: all). Returns a list of result dicts."""
    return [run_scenario(n, project_input) for n in (names or [s.name for s in SCENARIOS])]
