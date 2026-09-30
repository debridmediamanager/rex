#!/usr/bin/env python3
"""Rules found by the differential path-segment and header sweeps (`vsweep.py`), each
checked against the exchange a real PMS gave for the same request.

The expected values live in `tests/fixtures/parity_sweep_vb.jsonl`, one redacted
exchange per line, recorded off kids (1.43.4) or zen (1.42.2). A reference's ratingKey is
mapped onto the fixture catalog's item of the same kind before the request is replayed.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_sweep_vb.py
"""
import gzip
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "parity_sweep_vb.jsonl")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

# a reference's item -> the fixture catalog's item of the same kind
KIDS_ITEMS = {"1": "1007", "8": "1001"}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def fixture(rule):
    with open(FIXTURE) as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return [row for row in rows if row["rule"] == rule]


def local_path(path):
    return re.sub(r"(/metadata/)(\d+)", lambda m: m.group(1) + KIDS_ITEMS.get(m.group(2), m.group(2)),
                  path)


def send(port, method, path, headers=()):
    """One request on its own connection; the answer as (status, [(name, value)], body)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    connection.putheader("Host", "127.0.0.1:%d" % port)
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    connection.close()
    if (response.getheader("Content-Encoding") or "") == "gzip" and body:
        body = gzip.decompress(body)
    return response.status, response.getheaders(), body


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            if send(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def header(pairs, name):
    values = [value for key, value in pairs if key.lower() == name.lower()]
    return values or None


def replay(port, rule, what, headers=()):
    """Every fixture exchange of RULE: the same status, the same (decoded) body, and the
    same values of HEADERS -- absent where the reference sends none."""
    for row in fixture(rule):
        req, ref = row["request"], row["response"]
        path = local_path(req["path"])
        status, got, body = send(port, req["method"], path, req["headers"])
        want = {name: header(ref["headers"], name) for name in headers}
        have = {name: header(got, name) for name in headers}
        check("%s: %s %s is the measured %d" % (what, req["method"], path, ref["status"]),
              status == ref["status"] and body.decode("utf-8", "replace") == ref["body"]
              and want == have,
              "%s %r %s, measured %s" % (status, body[:200], have, want))


def run(port):
    # 1. A leaf the server does not route under `/hubs/sections/{id}/` is the Not Found
    #    document, not the section's Recommended page.
    replay(port, "hub-leaf", "an unknown section hub leaf")

    # 2. A `/web/*` 404 is the error document, gzipped like any other when gzip is
    #    offered (only the 200 asset is never gzipped); `/web/` is the same 302 as `/web`.
    replay(port, "web-404-gzip", "a missing web asset", headers=(
        "Content-Encoding", "X-Plex-Content-Original-Length",
        "X-Plex-Content-Compressed-Length", "Content-Length", "Connection", "Cache-Control"))

    # 3. `/children` of a leaf -- an episode as well as a movie -- is the 400.
    replay(port, "leaf-children", "a leaf's children")


def main():
    process, port = start()
    try:
        run(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d sweep parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
