#!/usr/bin/env python3
"""Cycle 87, territory B: a collection EMPTIED by removing its members keeps its last counts.

Cycle 87's python-plexapi walk on plexscratch42 (PMS 1.42.2) ran the plain-collection steps
for the first time since cycle 82 (plexscratch42 had refused the create with a busy-database
500 every cycle since), and `coll.removeItems` on the last member left the reference row
reading `childCount` 1 where both servers dropped the field. Re-measured and recorded in
`tests/fixtures/collection_emptied_s42.json`: a plain movie collection over Elephants Dream
(2006) and Sintel (2010), emptied one member at a time, keeps the count and the year span
it had with ONE member left -- `childCount` 1, `minYear`/`maxYear` the last member's year --
on the DELETE's own answer, `/library/collections/{id}`, `/library/metadata/{id}`, section
`/collections` and `all?type=18`, while `/children` lists nothing. A re-add and a second
emptying keep the re-added member's year; removing a one-member collection's tag from the
member (`collection[].tag.tag-`) does the same. A collection CREATED with nothing in it still
has no `childCount` (cycle 83); the reference simply stops recomputing the aggregate when the
count reaches zero.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle87_b.py
"""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "collection_emptied_s42.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "parity-cycle87-b", "Accept": "application/json"}
SECTION = "1"
A, B = "1012", "1013"          # catalog_ui movies of different years (2022, 2024)
FIELDS = ("childCount", "minYear", "maxYear")

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
        body = response.read()
        try:
            return response.status, json.loads(body) if body.strip() else None
        except ValueError:
            return response.status, None
    finally:
        connection.close()


def start(port=None, state=None):
    port = port or free_port()
    args = ["--port", port, "--catalog", CATALOG, "--assert-claimed", "--gdm", "off"]
    if state:
        args += ["--state", state]
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


def collection_rows(body, rating_key=None):
    rows = [row for row in ((body or {}).get("MediaContainer") or {}).get("Metadata") or []
            if row.get("type") == "collection"]
    if rating_key is not None:
        rows = [row for row in rows if str(row.get("ratingKey")) == str(rating_key)]
    return rows


def counts(row):
    return {name: row.get(name) for name in FIELDS}


def reference_checks():
    """What plexscratch42 answered, read back out of the recorded exchanges."""
    fixture = json.load(open(FIXTURE))
    year = {name: str(member["year"]) for name, member in fixture["members"].items()}
    exchanges = fixture["exchanges"]
    emptied = collection_rows(exchanges[0]["body"])[0]["ratingKey"]
    stale_b = {"childCount": 1, "minYear": year["B"], "maxYear": year["B"]}
    check("reference: the create over two members says 2, %s-%s" % (year["A"], year["B"]),
          counts(collection_rows(exchanges[0]["body"])[0])
          == {"childCount": 2, "minYear": year["A"], "maxYear": year["B"]},
          exchanges[0]["body"])
    last = [e for e in exchanges if e["method"] == "DELETE"
            and e["path"].endswith("/items/%s" % fixture["members"]["B"]["ratingKey"])][0]
    check("reference: the DELETE of the last member answers the one-member counts",
          counts(collection_rows(last["body"])[0]) == stale_b, last["body"])
    after = exchanges[exchanges.index(last) + 1:exchanges.index(last) + 6]
    for exchange in after[:4]:
        rows = collection_rows(exchange["body"], emptied)
        check("reference: %s keeps childCount 1 and %s" % (exchange["path"], year["B"]),
              rows and counts(rows[0]) == stale_b, rows)
    check("reference: /children lists nothing",
          after[4]["path"].endswith("/children")
          and not (after[4]["body"]["MediaContainer"].get("Metadata") or []), after[4]["body"])
    regained = exchanges[exchanges.index(last) + 7]
    again = exchanges[exchanges.index(last) + 9]
    stale_a = {"childCount": 1, "minYear": year["A"], "maxYear": year["A"]}
    check("reference: a re-add reads 1, %s" % year["A"],
          counts(collection_rows(regained["body"])[0]) == stale_a, regained["body"])
    check("reference: emptied again it keeps 1, %s" % year["A"],
          counts(collection_rows(again["body"])[0]) == stale_a, again["body"])
    untagged = [e for e in exchanges if e["method"] == "GET"][-2]
    check("reference: removing the member's Collection tag keeps 1, %s" % year["B"],
          counts(collection_rows(untagged["body"])[0]) == stale_b, untagged["body"])


def uri(port, *keys):
    machine = request(port, "GET", "/identity")[1]["MediaContainer"]["machineIdentifier"]
    return "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, ",".join(keys))


def create(port, title, *keys):
    status, body = request(port, "POST", "/library/collections?" + urllib.parse.urlencode(
        dict(type=1, title=title, smart=0, sectionId=SECTION, uri=uri(port, *keys))))
    return collection_rows(body)[0]["ratingKey"]


def reads(port, key):
    out = []
    for path in ("/library/collections/%s" % key, "/library/metadata/%s" % key,
                 "/library/sections/%s/collections" % SECTION,
                 "/library/sections/%s/all?type=18" % SECTION):
        rows = collection_rows(request(port, "GET", path)[1], key)
        out.append((path, counts(rows[0]) if rows else None))
    return out


def plexsim_checks(port):
    year = {}
    for key in (A, B):
        row = request(port, "GET", "/library/metadata/%s" % key)[1]["MediaContainer"]["Metadata"][0]
        year[key] = str(row["year"])
    check("the two catalog movies have different years", year[A] != year[B], year)
    key = create(port, "C87B Emptied", A, B)
    request(port, "DELETE", "/library/collections/%s/items/%s" % (key, A))
    status, body = request(port, "DELETE", "/library/collections/%s/items/%s" % (key, B))
    stale_b = {"childCount": 1, "minYear": year[B], "maxYear": year[B]}
    rows = collection_rows(body)
    check("the DELETE of the last member answers childCount 1 and %s" % year[B],
          status == 200 and rows and counts(rows[0]) == stale_b, (status, rows))
    for path, seen in reads(port, key):
        check("emptied: %s keeps childCount 1 and %s" % (path, year[B]), seen == stale_b, seen)
    children = request(port, "GET", "/library/collections/%s/children" % key)[1]
    check("emptied: /children lists nothing",
          not (children["MediaContainer"].get("Metadata") or []), children)
    request(port, "PUT", "/library/collections/%s/items?uri=%s"
            % (key, urllib.parse.quote(uri(port, A))))
    stale_a = {"childCount": 1, "minYear": year[A], "maxYear": year[A]}
    check("a re-add reads 1 and %s" % year[A], reads(port, key)[0][1] == stale_a, reads(port, key))
    request(port, "DELETE", "/library/collections/%s/items/%s" % (key, A))
    check("emptied again it keeps 1 and %s" % year[A], reads(port, key)[0][1] == stale_a,
          reads(port, key))
    untagged = create(port, "C87B Untagged", B)
    status, _ = request(port, "PUT", "/library/sections/%s/all?" % SECTION + urllib.parse.urlencode(
        {"type": 1, "id": B, "collection[].tag.tag-": "C87B Untagged"}))
    check("removing the member's Collection tag keeps 1 and %s" % year[B],
          status == 200 and reads(port, untagged)[0][1] == stale_b, reads(port, untagged))
    return key, stale_a


def created_empty_checks(port):
    """Cycle 83's rule beside it: a collection that never had a member has no childCount."""
    status, body = request(port, "POST", "/library/collections?" + urllib.parse.urlencode(
        dict(type=4, title="C87B Never", smart=0, sectionId="2",
             uri=uri(port, "20001"))))
    rows = collection_rows(body)
    check("a collection created with nothing in it still carries no childCount",
          rows and "childCount" not in rows[0] and "minYear" not in rows[0], rows)


def main():
    reference_checks()
    process, port = start()
    try:
        plexsim_checks(port)
        created_empty_checks(port)
    finally:
        stop(process)
    # The emptied counts are stored state: they live in the reference's database, so a
    # restart on --state must keep them.
    with tempfile.TemporaryDirectory() as tmp:
        state = os.path.join(tmp, "state.pickle")
        process, port = start(state=state)
        try:
            key, stale = plexsim_checks(port)
        finally:
            stop(process)
        process, port = start(port=port, state=state)
        try:
            check("after a --state restart the emptied collection keeps 1 and %s"
                  % stale["minYear"], reads(port, key)[0][1] == stale, reads(port, key))
        finally:
            stop(process)
    passed = sum(results)
    print("%s %d/%d cycle 87 B emptied-collection checks"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
