#!/usr/bin/env python3
"""Regression check for readiness polling's full deadline budget."""

import sys
import time

from startup_wait import poll_until


def main():
    started = time.monotonic()
    # The old fixed-attempt optimization would stop after roughly four seconds
    # (80 attempts at 50 ms), while the original budget was twenty seconds.
    ready_at = started + 4.25
    ready = poll_until(lambda: time.monotonic() >= ready_at, 20, 0.05)
    elapsed = time.monotonic() - started
    ok = ready and elapsed >= 4.0
    print("PASS delayed readiness keeps the full monotonic polling budget" if ok
          else "FAIL delayed readiness kept only the shortened polling window")
    print("PASS 1/1 startup readiness checks" if ok else "FAIL 0/1 startup readiness checks")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
