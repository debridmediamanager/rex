"""Small polling primitive for test-process readiness checks."""

import time


def poll_until(attempt, timeout, interval=0.05):
    """Retry an attempt until it succeeds or the monotonic budget expires."""
    deadline = time.monotonic() + timeout
    while True:
        if attempt():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))
