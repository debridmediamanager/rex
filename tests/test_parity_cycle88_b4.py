#!/usr/bin/env python3
"""Cycle 88, B4: a LISTING row carries two `Collection` tags, the first two by title.

`tests/fixtures/collection_listing_order_s42.json` (plexscratch42, 1.42.2) and
`collection_listing_order_s43.json` (plexscratch, 1.43.4) are one movie put into nine
collections one at a time -- titled so that creation order, plain title order, natural order
and a Unicode-aware fold all disagree -- with the Plex Web grid read after each create, then
every listing surface read once (python-plexapi's `section.all()` shape included), and on
1.43.4 a show put into three. Both versions answer alike:

- the DETAIL row carries every tag, in the order the item joined them;
- every LIGHT row -- the section grid with or without `includeCollections=1`, `/library/all`,
  both search endpoints, `/hubs/search`, `recentlyAdded`, a collection's `/children` on both
  paths, a show's grid row -- carries exactly TWO, the first two by title compared with ASCII
  case folded and nothing else: `aardvark` before `Alpha`, `10x` before `9x`, `0F` before `0é`.

Both servers sent every tag in join order, so python-plexapi's `Movie.collections` on a
`section.all()` object (a non-empty list is never reloaded) named different collections.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle88_b4.py
"""
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURES = [os.path.join(HERE, "fixtures", "collection_listing_order_s%s.json" % v)
            for v in ("42", "43")]
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "parity-cycle88-b4", "Accept": "application/json"}
MOVIE, SHOW = "1012", "20001"      # catalog_ui's movie and show
QUERY = "Trust"                    # a title word only MOVIE carries

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=dict(HEADERS, Connection="close", **(headers or {})))
        response = connection.getresponse()
        data = response.read()
        try:
            return response.status, json.loads(data) if data.strip() else None
        except ValueError:
            return response.status, None
    finally:
        connection.close()


def start():
    port = free_port()
    args = ["--port", port, "--catalog", CATALOG, "--assert-claimed", "--gdm", "off"]
    process = subprocess.Popen(server_command(*args, root=ROOT), cwd=ROOT, env=ENV,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if process.poll() is not None:
            break
        try:
            if request(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def stop(process):
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def rows(body):
    container = (body or {}).get("MediaContainer", {})
    found = list(container.get("Metadata") or [])
    for hub in container.get("Hub") or []:
        found += hub.get("Metadata") or []
    for result in container.get("SearchResult") or []:
        if result.get("Metadata"):
            found.append(result["Metadata"])
    return found


def replay(port, machine, fixture, label):
    """Reissue the fixture's requests with this server's keys; compare the member's tags."""
    tag = fixture["tag"]
    members = {fixture["member"]: MOVIE}
    if fixture.get("show"):
        members[fixture["show"]] = SHOW
    keys = dict(members)
    titles = {}
    step = "before any create"

    def local(path, method):
        path = re.sub(r"(/library/(?:metadata|collections)/)(\d+)",
                      lambda m: m.group(1) + keys.get(m.group(2), m.group(2)), path)
        for ref, mine in members.items():
            path = path.replace("metadata%2F" + ref, "metadata%2F" + mine)
        if method == "GET":     # a search term, not a create's title
            path = re.sub(r"((?:title|query)=)[^&]+", lambda m: m.group(1) + QUERY, path)
        return path.replace("server%3A%2F%2FMACHINE", "server%3A%2F%2F" + machine)

    for exchange in fixture["exchanges"]:
        status, body = request(port, exchange["method"], local(exchange["path"], exchange["method"]),
                               exchange.get("headers"))
        if exchange["method"] == "POST":
            created = exchange["created"]
            keys[str(created["ratingKey"])] = str(rows(body)[0].get("ratingKey")) if rows(body) else "?"
            check("%s create %r answers %s" % (label, created["title"], exchange["status"]),
                  status == exchange["status"], status)
            step = "after %r" % created["title"]
            continue
        path = exchange["path"].split("?")[0] + ("?" + exchange["path"].split("?", 1)[1][:40]
                                                 if "?" in exchange["path"] else "")
        for want_row in exchange["rows"]:
            want = [t["tag"] for t in want_row.get("Collection") or [] if tag in t["tag"].lower()]
            mine_key = members[str(want_row["ratingKey"])]
            got_rows = [r for r in rows(body) if str(r.get("ratingKey")) == mine_key]
            got = ([t.get("tag") for t in got_rows[0].get("Collection") or []
                    if tag in str(t.get("tag")).lower()] if got_rows else None)
            check("%s %s %s: the %s row's collections are %s"
                  % (label, step, path, want_row["type"], [w.split(" ", 1)[1] for w in want]),
                  got == want, got)


def main():
    for path in FIXTURES:
        fixture = json.load(open(path, encoding="utf-8"))
        process, port = start()
        try:
            machine = request(port, "GET", "/identity")[1]["MediaContainer"]["machineIdentifier"]
            replay(port, machine, fixture, os.path.basename(path)[-8:-5])
        finally:
            stop(process)
    passed = sum(results)
    print("PASS %d/%d" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
