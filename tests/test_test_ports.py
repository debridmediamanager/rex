#!/usr/bin/env python3
"""Deterministic checks for the bounded test-port allocator."""

import sys

from test_ports import DEFAULT_HIGH, DEFAULT_LOW, MAX_PORT, PortPool, parse_range


def main():
    checks = []
    checks.append(parse_range(None) == (DEFAULT_LOW, DEFAULT_HIGH)
                  and parse_range("20001-20003") == (20001, 20003)
                  and DEFAULT_HIGH <= MAX_PORT)

    pool = PortPool("20000-20002", probe=lambda _port: False)
    allocated = [pool.free_port() for _ in range(3)]
    checks.append(allocated == [20000, 20001, 20002] and len(set(allocated)) == 3)

    try:
        pool.free_port()
    except RuntimeError:
        exhausted = True
    else:
        exhausted = False
    checks.append(exhausted)

    occupied = PortPool("20000-20002", probe=lambda port: port == 20000)
    checks.append(occupied.free_port() == 20001)

    invalid = ("0-1", "20001-20000", "20000", "20000-32768", "nope-20001", "20000-nope")
    invalid_checks = []
    for value in invalid:
        try:
            parse_range(value)
        except ValueError:
            invalid_checks.append(True)
        else:
            invalid_checks.append(False)
    checks.append(all(invalid_checks))

    passed = sum(checks)
    print("PASS %d/%d bounded port allocator checks" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
