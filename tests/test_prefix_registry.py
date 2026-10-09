#!/usr/bin/env python3
"""Exact replay of the measured legacy-dispatcher prefix registry.

The fixture is a fresh Cycle 50 replay of Cycle 49's actually executed list.
It preserves root attribute order, row order, row element names and row attribute
order; this is deliberately a finite captured registry, not a prefix algorithm.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from xml.parsers import expat

from server_command import server_command


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "prefix_registry.json")


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def parse_xml(data):
    stack = []
    root = None
    parser = expat.ParserCreate()
    parser.ordered_attributes = True

    def start(name, attrs):
        nonlocal root
        node = {
            "tag": name,
            "attrs": [[attrs[i], attrs[i + 1]] for i in range(0, len(attrs), 2)],
            "children": [],
        }
        if stack:
            stack[-1]["children"].append(node)
        else:
            root = node
        stack.append(node)

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda _name: stack.pop()
    parser.Parse(data, True)
    return root


def main():
    measured = json.load(open(FIXTURE))
    cases = measured["cases"]
    if len(cases) != 58 or len({case["path"] for case in cases}) != 58:
        raise SystemExit("fixture denominator is not the executed 58-path list")

    port = free_port()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                        "--gdm", "off", root=ROOT),
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                probe = socket.create_connection(("127.0.0.1", port), timeout=1)
                probe.close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit("plexsim did not start")

        failures = []
        for case in cases:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                # No device identity headers: this is the exact safe family shape.
                conn.request("GET", case["path"], headers={"Accept": "application/xml"})
                response = conn.getresponse()
                body = response.read()
            finally:
                conn.close()
            got = parse_xml(body) if response.status == 200 and body.lstrip().startswith(b"<") else None
            if response.status != 200 or got != case["root"]:
                failures.append({"path": case["path"], "status": response.status,
                                 "got": got, "want": case["root"]})

        if failures:
            print("FAIL %d/58 exact prefix shapes" % len(failures))
            for failure in failures[:8]:
                print("  %s status=%s\n    got=%r\n    want=%r" % (
                    failure["path"], failure["status"], failure["got"], failure["want"]))
            return 1
        print("PASS 58/58 exact prefix shapes (58 requests, ordered root attrs/rows/row attrs)")
        return 0
    finally:
        proc.kill()
        proc.wait()


if __name__ == "__main__":
    sys.exit(main())
