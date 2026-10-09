#!/usr/bin/env python3
"""Cycle 90, territory N (axis 11 over HTTP): what the analyst's comparison found.

An unseeded `sort=random` -- Plex Web's "Randomly", the Android app's grid, the music
hubs -- is a fresh UNIFORM order per request on a real PMS. The fixture holds 60
consecutive reads of the app's own grid request on two references, every order kept.
plexsim ordered by the CRC-32 of `<salt>/<ratingKey>` with a fresh salt: CRC is affine,
so a new salt only XORs every row's key with one mask, and six movies came out in 8 of
720 orders -- the "reshuffled" grid repeated itself every few reads.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle90_n.py
"""
import http.client
import json
import math
import os
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "c90_n_unseeded_random_orders.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-c90n",
           "X-Plex-Product": "Plex for Android (Mobile)", "X-Plex-Version": "2026.9.0",
           "X-Plex-Platform": "Android",
           "X-Plex-Container-Start": "0", "X-Plex-Container-Size": "30"}

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


def request(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers=dict(HEADERS, Connection="close"))
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    return response.status, json.loads(raw) if raw else None


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
            if request(port, "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def uniform_distinct(reads, items):
    """How many distinct orders `reads` uniform draws of `items` rows are expected to give."""
    orders = math.factorial(items)
    return -orders * math.expm1(reads * math.log1p(-1 / orders))


def adjacent_always(draws):
    first = draws[0]
    return [(a, b) for a in first for b in first if a < b
            and all(abs(list(d).index(a) - list(d).index(b)) == 1 for d in draws)]


def run(port):
    fixture = json.load(open(FIXTURE))
    query = "&".join("%s=%s" % tuple(pair) for pair in fixture["request"]["query"])
    path = "%s?%s" % (fixture["request"]["path"], query)
    # The references first: the bar below is what THEY clear, measured.
    for host, orders in sorted(fixture["orders"].items()):
        items = len(orders[0])
        distinct = len(set(map(tuple, orders)))
        expected = uniform_distinct(len(orders), items)
        check("%s (measured): %d reads of %d rows give %d distinct orders, a uniform draw's %.1f"
              % (host, len(orders), items, distinct, expected),
              distinct >= 0.8 * expected and not adjacent_always(orders)
              and all(sorted(o) == sorted(orders[0]) for o in orders), distinct)
    reads = len(fixture["orders"]["kids"])
    drawn = []
    for _ in range(reads):
        status, body = request(port, path)
        drawn.append(tuple(row["ratingKey"] for row in (body or {}).get(
            "MediaContainer", {}).get("Metadata") or []))
    items = len(drawn[0])
    distinct = len(set(drawn))
    expected = uniform_distinct(reads, items)
    check("an unseeded sort=random is a uniform draw: %d reads of %d rows, %d distinct orders "
          "(a uniform draw gives %.1f)" % (reads, items, distinct, expected),
          items >= 5 and distinct >= 0.8 * expected
          and all(sorted(d) == sorted(drawn[0]) for d in drawn), sorted(set(drawn))[:10])
    # The XOR-mask order keeps the rows whose keys share the longest prefix side by side in
    # EVERY draw; a uniform order puts a given pair together 2/n of the time.
    always = adjacent_always(drawn)
    check("no two rows are adjacent in every draw", not always, always)
    # A seed still holds one order (measured, cycle 13): unchanged by this fix.
    seeded = {tuple(row["ratingKey"] for row in request(port, fixture["request"]["path"]
                    + "?type=1&sort=random:12345")[1]["MediaContainer"]["Metadata"])
              for _ in range(5)}
    check("random:<seed> is still one stable order", len(seeded) == 1, seeded)


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
    print("PASS %d/%d cycle 90 N checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
