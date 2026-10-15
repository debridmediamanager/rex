#!/usr/bin/env python3
"""Bounded loopback ports for isolated test processes.

The normal operating-system ephemeral allocator can return ports above the
reference server's range.  Test suites use this small allocator instead so a
runner can give each process a disjoint ``REX_TEST_PORTS=low-high`` range.
"""

import os
import socket
import threading


MAX_PORT = 32767
DEFAULT_LOW = 20000
DEFAULT_HIGH = MAX_PORT
# The native runtime smoke test deliberately owns this fixed port.
RESERVED_PORTS = frozenset((32620,))


def parse_range(value):
    """Return a validated inclusive port range from ``low-high``."""
    if value is None or value == "":
        return DEFAULT_LOW, DEFAULT_HIGH
    if not isinstance(value, str):
        raise ValueError("REX_TEST_PORTS must be low-high")
    parts = value.strip().split("-")
    if len(parts) != 2:
        raise ValueError("REX_TEST_PORTS must be low-high")
    try:
        low, high = (int(part) for part in parts)
    except ValueError as error:
        raise ValueError("REX_TEST_PORTS must contain integer bounds") from error
    if not (1 <= low <= high <= MAX_PORT):
        raise ValueError("REX_TEST_PORTS must stay between 1 and %d" % MAX_PORT)
    return low, high


def _occupied(port):
    """Return whether a port is already in use on loopback."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.05):
            return True
    except OSError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # The Python TCPServer callers do not set allow_reuse_address. Matching
        # their bind semantics also skips ports still held by TIME_WAIT sockets
        # from a preceding suite run.
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


class PortPool:
    """Allocate each available port at most once in this process."""

    def __init__(self, value=None, probe=None, reserved=RESERVED_PORTS):
        self.low, self.high = parse_range(value)
        self.range_text = "%d-%d" % (self.low, self.high)
        self._probe = _occupied if probe is None else probe
        self._reserved = frozenset(reserved)
        self._returned = set()
        self._cursor = self.low
        self._lock = threading.Lock()

    def free_port(self):
        with self._lock:
            size = self.high - self.low + 1
            for _ in range(size):
                port = self._cursor
                self._cursor = self.low if port == self.high else port + 1
                if port in self._returned or port in self._reserved:
                    continue
                if self._probe(port):
                    continue
                self._returned.add(port)
                return port
        raise RuntimeError("no free test port in %s" % self.range_text)


_default_lock = threading.Lock()
_default_pools = {}


def free_port():
    """Allocate a bounded, non-reused loopback port for this process."""
    value = os.environ.get("REX_TEST_PORTS")
    with _default_lock:
        pool = _default_pools.get(value)
        if pool is None:
            pool = _default_pools[value] = PortPool(value)
        return pool.free_port()
