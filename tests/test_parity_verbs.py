#!/usr/bin/env python3
"""The verb table: which verbs each read route family takes, replayed from the
measured record in `tests/fixtures/verb_table_2026-09-30.json`.

A differential sweep sent HEAD, OPTIONS, PUT, POST and DELETE to every client read
path on the oracle and the native server, and the two disagreed by FAMILY. Each rule
below is the reference's answer for a whole family, measured on plexscratch (1.43.4)
for every verb and on plexlab and the 1.42.2 reference for OPTIONS and HEAD:

- section leaves  -- `/library/sections/{id}/{leaf}` takes the verbs of cycle 15's
                     leaf table; every read leaf and the section's own POST is a 404
                     (the native server answered them the GET body);
- section prefs   -- HEAD is the GET (the native server 404'd it);
- listings        -- `/status/sessions/background` and `/library/search` refuse every
                     verb but GET and HEAD, as `/status/sessions` does (the oracle
                     answered the GET body);
- statistics      -- the whole `/statistics` subtree is read-only (the oracle
                     answered `/statistics/resources` to every verb);
- key list        -- `/library/metadata/{a},{b}` takes one item's verbs, so POST and
                     OPTIONS are 404s (the oracle answered the listing to POST);
- provider groups -- the group LISTING's PUT and DELETE are 400s (the oracle answered
                     the listing, the native server 404'd);
- background list -- `/playlists?type=42` is a read: POST is the create's 400 and PUT,
                     DELETE and OPTIONS the dispatcher (the oracle answered the POST
                     with the list);
- player          -- a bare OPTIONS under `/player/` is the same 400 as every other
                     verb (the oracle 404'd it).

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `REX_SERVER=python`.

    python3 tests/test_parity_verbs.py
"""
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
FIXTURE = os.path.join(HERE, "fixtures", "verb_table_2026-09-30.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-verbs"}

# plexscratch's two movies (ratingKeys 1 and 3) are two movies of the fixture catalog.
ITEM = {"1": "1007", "3": "1008"}

RULES = (
    ("section prefs HEAD", lambda m, p: m == "HEAD" and re.match(r"/library/sections/\d+/prefs", p)),
    ("section leaves", lambda m, p: p.startswith("/library/sections/")),
    ("listings", lambda m, p: p.startswith(("/status/sessions", "/library/search"))),
    ("statistics", lambda m, p: p.startswith("/statistics")),
    ("key list", lambda m, p: p.startswith("/library/metadata/")),
    ("provider groups", lambda m, p: p.startswith("/media/providers")),
    ("background list", lambda m, p: p.startswith("/playlists")),
    ("player", lambda m, p: p.startswith("/player/")),
)

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


def request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=dict(HEADERS, Connection="close"))
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    return response.status, (response.getheader("Content-Type") or "").split(";")[0], raw


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
            if request(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def local_path(path):
    route, sep, query = path.partition("?")
    match = re.match(r"^/library/metadata/([\d,]+)$", route)
    if match:
        route = "/library/metadata/" + ",".join(ITEM[key] for key in match.group(1).split(","))
    return route + sep + query


def rule_of(method, path):
    for name, test in RULES:
        if test(method, path):
            return name
    return "other"


def run(port):
    rows = json.load(open(FIXTURE))["rows"]
    # One answer per (method, path): the three references agree wherever two measured
    # the same cell, and the check names each witness.
    cells = {}
    for row in rows:
        cells.setdefault((row["method"], row["path"]), []).append(row)
    for (method, path), witnesses in sorted(cells.items(), key=lambda kv: (rule_of(*kv[0]), kv[0])):
        want = witnesses[0]
        assert all(w["status"] == want["status"] for w in witnesses), witnesses
        status, ctype, raw = request(port, method, local_path(path))
        ok = status == want["status"]
        detail = "want %d, got %d" % (want["status"], status)
        if ok and status >= 400:
            # Every refusal here is the ordinary HTML error document.
            ok = ctype == "text/html"
            detail = "refusal carried %r" % ctype
        if ok and "body" in want:
            try:
                got = json.loads(raw)
            except ValueError:
                got = raw[:120]
            ok = got == want["body"]
            detail = "body %r" % (got,)
        check("%s: %s %s -> %d (%s)" % (rule_of(method, path), method, path, want["status"],
                                        "/".join(sorted({w["reference"] for w in witnesses}))),
              ok, detail)


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
    print("PASS %d/%d verb-table checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
