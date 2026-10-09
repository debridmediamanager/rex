#!/usr/bin/env python3
"""Cycle 88, B3: a collection's delete untags its members by its CURRENT title.

`tests/fixtures/collection_delete_by_title_s42.json` (plexscratch42, 1.42.2) and
`collection_delete_by_title_s43.json` (plexscratch, 1.43.4) are the same five sequences
over one movie, and the two versions answer every step alike:

- renamed on the ITEM path (`PUT /library/metadata/{id}?title.value=`) or on the
  COLLECTIONS path (`PUT /library/collections/{id}?title=`, test_ui_surface's shape), the
  members keep the tag under the old name, and the delete then leaves it on them;
- renamed on the SECTION path the tag is renamed too, and the delete takes it;
- renamed away and back -- in another case -- the delete takes it (the match ignores case);
- B renamed on the item path onto A's title: deleting B strips A's tag from the member, so
  A lists nobody and keeps the counts it had (cycle 87's emptied-collection rule).

Both servers untagged by the stored tag, so the first two left nothing where a real PMS
leaves the tag, and the last left A's tag and membership alone.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle88_b3.py
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

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURES = [os.path.join(HERE, "fixtures", "collection_delete_by_title_s%s.json" % v)
            for v in ("42", "43")]
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "parity-cycle88-b3", "Accept": "application/json"}
SECTION, MEMBER = "1", "1012"      # catalog_ui's movie library and a movie in it

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


def request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=dict(HEADERS, Connection="close"))
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


def first(body):
    return ((body or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0]


def replay(port, machine, fixture, label):
    """Reissue the fixture's requests with this server's keys; compare what it measured."""
    tag, ref_member, ref_section = fixture["tag"], fixture["member"], fixture["section"]
    mine = lambda body: [t.get("tag") for t in first(body).get("Collection") or []
                         if tag in str(t.get("tag")).lower()]
    for sequence in fixture["sequences"]:
        keys = {}
        step = "the create"

        def local(path):
            path = re.sub(r"(/library/(?:metadata|collections)/)(\d+)",
                          lambda m: m.group(1) + (MEMBER if m.group(2) == ref_member
                                                  else keys.get(m.group(2), m.group(2))), path)
            path = re.sub(r"([?&]id=)(\d+)", lambda m: m.group(1) + keys.get(m.group(2), m.group(2)), path)
            path = path.replace("/sections/%s/" % ref_section, "/sections/%s/" % SECTION)
            path = path.replace("sectionId=%s&" % ref_section, "sectionId=%s&" % SECTION)
            return (path.replace("server%3A%2F%2FMACHINE", "server%3A%2F%2F" + machine)
                    .replace("metadata%2F" + ref_member, "metadata%2F" + MEMBER))

        for exchange in sequence["exchanges"]:
            method, path, want_body = exchange["method"], exchange["path"], exchange["body"]
            status, body = request(port, method, local(path))
            name = "%s %s: %s" % (label, sequence["name"], step)
            if method == "POST":
                keys[str(first(want_body)["ratingKey"])] = str(first(body).get("ratingKey"))
                check("%s answers %s" % (name, exchange["status"]), status == exchange["status"], status)
                step = "after create %r" % first(want_body).get("title")
                continue
            if method != "GET":
                step = "after %s %s" % (method, re.sub(r"\d+", "{id}", path.split("?")[0]))
                check("%s answers %s" % (name, exchange["status"]), status == exchange["status"], status)
                continue
            row = first(want_body)
            if row.get("type") == "movie":
                want = [t["tag"] for t in row.get("Collection") or [] if tag in t["tag"].lower()]
                check("%s the member's collections are %s" % (name, want), mine(body) == want, mine(body))
            elif "/children" in path:
                want = want_body["MediaContainer"].get("size")
                got = (body or {}).get("MediaContainer", {}).get("size")
                check("%s the other collection lists %s member(s)" % (name, want), got == want, got)
            else:
                want = {k: row.get(k) for k in ("title", "childCount")}
                got = {k: first(body).get(k) for k in ("title", "childCount")}
                check("%s the other collection reads %s" % (name, want), got == want, got)


def main():
    for path in FIXTURES:
        # A fresh server per fixture: both recorded the same titles, and the first replay's
        # tags stay on the member, as they did on the reference.
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
