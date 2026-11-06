"""
CHAOS TEST - break the pipeline on purpose and prove it fails safely and recovers.

    python chaos_test.py                        run all 13 scenarios
    python chaos_test.py --list                 show the scenarios
    python chaos_test.py --scenario code_crash  run one (repeat --scenario for more)

Exit code 0 = every scenario behaved as expected. See pipeline/chaos.py for how it works.
"""
import argparse
import logging
import sys

from pipeline import chaos
from pipeline.logger import LOGGER_NAME


def main(argv=None):
    parser = argparse.ArgumentParser(description="Customer 360 chaos tests")
    parser.add_argument("--list", action="store_true", help="list the scenarios and exit")
    parser.add_argument("--scenario", action="append", choices=list(chaos.BY_NAME), help="run only this scenario")
    args = parser.parse_args(argv)

    if args.list:
        for s in chaos.SCENARIOS:
            print(f"  {s.name:<22} exit {s.expected_exit}  {s.fault}")
        return 0

    # The pipeline's own INFO logs would drown the report; they still go to each sandbox's log files.
    logging.getLogger(LOGGER_NAME).disabled = True
    print(f"{'scenario':<22} {'fault':<42} {'exit':>4}  {'result':<6} recovery")
    print("-" * 120)
    results = []
    for name in args.scenario or [s.name for s in chaos.SCENARIOS]:
        r = chaos.run_scenario(name)
        results.append(r)
        print(f"{r['scenario']:<22} {r['fault'][:42]:<42} {r['actual_exit']:>4}  "
              f"{'PASS' if r['passed'] else 'FAIL':<6} {r['recovery']}")
        for c in r["checks"]:
            if not c["passed"]:
                print(f"{'':<22}   x {c['check']}: {c['detail']}")
    passed = sum(r["passed"] for r in results)
    print("-" * 120)
    print(f"{passed}/{len(results)} scenarios handled correctly")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
