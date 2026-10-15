#!/usr/bin/env python3
"""Regression checks for bounded suite scheduling and failure handling."""

import io
import os
import subprocess
import sys
import threading
import time

from parallel_suites import ROOT, assign_port_ranges, run_suites
from test_ports import MAX_PORT, parse_range


def main():
    names = ("instant-a", "instant-b", "slow-a", "slow-b", "fail")
    lock = threading.Lock()
    first_workers = threading.Barrier(2)
    active = 0
    peak = 0
    finished = []
    ranges = {}

    def fake_runner(name, port_range):
        nonlocal active, peak
        ranges[name] = port_range
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if name.startswith("instant"):
                first_workers.wait(timeout=1)
            if name.startswith("slow"):
                time.sleep(0.05)
            return (1 if name == "fail" else 0, "worker %s finished\n" % name)
        finally:
            with lock:
                active -= 1
                finished.append(name)

    output = io.StringIO()
    results = run_suites(names, workers=2, runner=fake_runner, stream=output)
    parsed_ranges = [parse_range(ranges[name]) for name in names]
    try:
        assign_port_ranges(("too-small-a", "too-small-b"), base=32760, size=8)
    except ValueError:
        capacity_rejected = True
    else:
        capacity_rejected = False
    child = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tests", "parallel_suites.py"), "--workers", "1",
         "test_parallel_failure_fixture"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    checks = [
        set(results) == set(names),
        results["fail"][0] == 1 and all(results[name][0] == 0 for name in names if name != "fail"),
        set(finished) == set(names),
        peak == 2,
        len(set(ranges.values())) == len(names)
        and all(low <= high <= MAX_PORT for low, high in parsed_ranges)
        and parsed_ranges == sorted(parsed_ranges),
        capacity_rejected,
        all("::group::%s" % name in output.getvalue()
            and "::endgroup::" in output.getvalue()
            and "%s exit " % name in output.getvalue() for name in names),
        child.returncode == 1 and "test_parallel_failure_fixture exit 7" in child.stdout,
    ]
    passed = sum(checks)
    print("PASS %d/%d parallel scheduler checks" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
