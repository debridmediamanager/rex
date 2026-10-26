"""Run independent Python suites with bounded, attributable concurrency."""

import argparse
import concurrent.futures
import os
import subprocess
import sys

from test_ports import MAX_PORT


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The suites run in the CI container's own network, so nothing else listens down here.
PORT_RANGE_BASE = 10000
PORT_RANGE_SIZE = 240


def assign_port_ranges(names, base=PORT_RANGE_BASE, size=PORT_RANGE_SIZE):
    """Give each suite a stable, disjoint bounded range."""
    names = list(names)
    if len(set(names)) != len(names):
        raise ValueError("suite names must be unique")
    if base < 1 or size < 1:
        raise ValueError("port range base and size must be positive")
    ranges = {}
    for index, name in enumerate(names):
        low = base + index * size
        high = low + size - 1
        if high > MAX_PORT:
            raise ValueError("not enough bounded ports for scheduled suites")
        ranges[name] = "%d-%d" % (low, high)
    return ranges


def execute_suite(name, port_range=None):
    env = dict(os.environ)
    if port_range is not None:
        env["REX_TEST_PORTS"] = port_range
    process = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tests", name + ".py")],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return process.returncode, process.stdout


def run_suites(names, workers=4, runner=execute_suite, stream=None):
    """Run every name, wait for every worker, and return {name: (code, output)}."""
    if workers < 1:
        raise ValueError("workers must be positive")
    names = list(names)
    results = {}
    if not names:
        return results
    port_ranges = assign_port_ranges(names)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(names))) as pool:
        futures = {pool.submit(runner, name, port_ranges[name]): name for name in names}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                code, output = future.result()
                results[name] = (int(code), str(output))
            except Exception as error:  # report runner failures as suite failures
                results[name] = (1, "runner failed: %s\n" % error)
            if stream is not None:
                code, output = results[name]
                print("::group::%s" % name, file=stream)
                if output:
                    print(output, end="" if output.endswith("\n") else "\n", file=stream)
                print("::endgroup::", file=stream)
                print("%s exit %d" % (name, code), file=stream)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("suites", nargs="+")
    args = parser.parse_args(argv)
    results = run_suites(args.suites, workers=args.workers, stream=sys.stdout)
    return 0 if all(code == 0 for code, _output in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
