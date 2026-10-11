#!/usr/bin/env python3
"""Focused regressions for playback-session timing."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("REX_TEST_ROOT", os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import sessions  # noqa: E402


def main():
    failures = []

    def check(name, condition, detail=""):
        print(("PASS" if condition else "FAIL"), name, detail)
        if not condition:
            failures.append(name)

    actual_time = sessions.time.time
    clock = [1000.0]
    sessions.time.time = lambda: clock[0]
    try:
        live = sessions.Sessions(ttl=180)
        live._sessions = {
            "older": {"sessionKey": "1", "seen": 1028.0},
            "newer": {"sessionKey": "2", "seen": 1031.0},
        }

        # The most recent global tick was 1180. At 1209 both rows are older than
        # 180 seconds by wall time, but the 1210 reaper has not run yet.
        clock[0] = 1209.0
        before_tick = [row["sessionKey"] for row in live.live()]
        clock[0] = 1210.0
        at_tick = [row["sessionKey"] for row in live.live()]
        clock[0] = 1240.0
        next_tick = [row["sessionKey"] for row in live.live()]
        check("sessions expire on the global 30-second reaper after age 180",
              before_tick == ["1", "2"] and at_tick == ["2"] and next_tick == [],
              "before=%r at=%r next=%r" % (before_tick, at_tick, next_tick))
    finally:
        sessions.time.time = actual_time

    if failures:
        print("FAIL %d session check(s)" % len(failures))
        return 1
    print("PASS all session checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
